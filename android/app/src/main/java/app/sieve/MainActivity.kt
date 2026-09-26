package app.sieve

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.content.DialogInterface
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.Gravity
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.progressindicator.CircularProgressIndicator
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Sieve's web interface in a WebView, served either by Sieve running on this
 * phone (SieveService) or by your own Sieve server, chosen on first launch
 * and changeable from the menu.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var refresh: SwipeRefreshLayout
    private lateinit var status: TextView
    private lateinit var root: FrameLayout
    // The screen shown while Sieve starts or when it cannot be reached: the
    // logo, a spinner or Try again / Change, and the message, on the web
    // app's background colour.
    private lateinit var statusPanel: LinearLayout
    private lateinit var spinner: CircularProgressIndicator
    private lateinit var statusActions: LinearLayout
    private lateinit var retryButton: MaterialButton
    private var fullscreenView: View? = null
    private var fullscreenCallback: WebChromeClient.CustomViewCallback? = null
    private var baseUrl: String = SieveService.LOCAL_URL
    private var pendingShare: String? = null

    private val prefs by lazy { getSharedPreferences("sieve", Context.MODE_PRIVATE) }

    // A WebView ignores <input type="file"> unless the app opens a picker for
    // it: without this, importing subscriptions, history, profiles or YouTube
    // cookies did nothing on the phone.
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val pickFile = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        fileCallback?.onReceiveValue(if (uri != null) arrayOf(uri) else null)
        fileCallback = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        root = FrameLayout(this)
        web = WebView(this).apply {
            // The page's own background, so there is no white flash before it
            // loads (most visible in the dark theme).
            setBackgroundColor(getColor(R.color.paper))
        }
        refresh = SwipeRefreshLayout(this).apply {
            addView(web)
            setColorSchemeColors(getColor(R.color.indigo))
            setProgressBackgroundColorSchemeColor(getColor(R.color.surface))
        }
        refresh.setOnRefreshListener { web.reload() }
        root.addView(refresh)
        root.addView(buildStatusPanel(), FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)
        configureWebView()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    fullscreenView != null -> exitFullscreen()
                    web.canGoBack() -> web.goBack()
                    else -> { isEnabled = false; onBackPressedDispatcher.onBackPressed() }
                }
            }
        })

        pendingShare = sharedUrl(intent)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        when (prefs.getString("mode", null)) {
            null -> chooseMode(firstRun = true)
            "remote" -> openRemote(prefs.getString("remote", "") ?: "")
            else -> openLocal()
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        sharedUrl(intent)?.let { web.loadUrl(siftUrl(it)) }
    }

    // ------------------------------------------------------------- modes

    private fun chooseMode(firstRun: Boolean) {
        // Two choice cards (layout/dialog_choose_mode.xml); the current one is
        // outlined when this is opened again from the menu.
        val view = layoutInflater.inflate(R.layout.dialog_choose_mode, null)
        val builder = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.choose_title)
            .setView(view)
            .setCancelable(!firstRun)
        if (!firstRun) builder.setNegativeButton(android.R.string.cancel, null)
        val dialog = builder.create()
        val current = prefs.getString("mode", null)
        view.findViewById<View>(R.id.option_local).apply {
            isActivated = !firstRun && current != "remote"
            setOnClickListener {
                dialog.dismiss()
                prefs.edit().putString("mode", "local").apply()
                openLocal()
            }
        }
        view.findViewById<View>(R.id.option_remote).apply {
            isActivated = !firstRun && current == "remote"
            setOnClickListener {
                dialog.dismiss()
                askServer()
            }
        }
        dialog.show()
    }

    private fun askServer() {
        val view = layoutInflater.inflate(R.layout.dialog_server, null)
        val input = view.findViewById<EditText>(R.id.server_input).apply {
            setText(prefs.getString("remote", ""))
            setSelection(text.length)
        }
        val backToChoice = {
            if (prefs.getString("mode", null) == null) chooseMode(firstRun = true)
        }
        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.server_title)
            .setMessage(R.string.server_message)
            .setView(view)
            .setPositiveButton(R.string.connect) { _, _ ->
                val url = input.text.toString().trim().trimEnd('/')
                    .let { if (it.startsWith("http")) it else "http://$it" }
                prefs.edit().putString("mode", "remote").putString("remote", url).apply()
                openRemote(url)
            }
            .setNegativeButton(android.R.string.cancel) { _, _ -> backToChoice() }
            // Back on the first run returns to the choice instead of leaving
            // an empty screen.
            .setOnCancelListener { backToChoice() }
            .create()
        // The keyboard's Go key connects, and the keyboard opens with the dialog.
        input.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_GO) {
                dialog.getButton(DialogInterface.BUTTON_POSITIVE).performClick(); true
            } else false
        }
        dialog.window?.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE)
        dialog.show()
        input.requestFocus()
    }

    private fun openLocal() {
        baseUrl = SieveService.LOCAL_URL
        showStatus(getString(R.string.starting))
        val service = Intent(this, SieveService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(service) else startService(service)
        waitThenLoad(baseUrl, seconds = 90)
    }

    private fun openRemote(url: String) {
        if (url.isBlank()) { askServer(); return }
        baseUrl = url
        showStatus(getString(R.string.connecting, url))
        waitThenLoad(url, seconds = 10)
    }

    private fun waitThenLoad(url: String, seconds: Int) {
        Thread {
            val deadline = System.currentTimeMillis() + seconds * 1000L
            var ok = false
            while (System.currentTimeMillis() < deadline && !ok) {
                ok = try {
                    (URL("$url/api/status").openConnection() as HttpURLConnection).run {
                        connectTimeout = 1500; readTimeout = 3000
                        val code = responseCode
                        disconnect()
                        code == 200
                    }
                } catch (e: Exception) { Thread.sleep(500); false }
            }
            runOnUiThread {
                if (ok) {
                    hideStatus()
                    web.loadUrl(pendingShare?.let { siftUrl(it) } ?: url)
                    pendingShare = null
                } else {
                    val retry = {
                        showStatus(if (url == SieveService.LOCAL_URL) getString(R.string.starting)
                                   else getString(R.string.connecting, url))
                        waitThenLoad(url, seconds)
                    }
                    // The screen keeps Try again / Change too, so closing the
                    // dialog no longer leaves a dead end.
                    showStatus(getString(R.string.unreachable, url), retry)
                    MaterialAlertDialogBuilder(this)
                        .setTitle(R.string.unreachable_title)
                        .setMessage(getString(R.string.unreachable, url))
                        .setPositiveButton(R.string.retry) { _, _ -> retry() }
                        .setNegativeButton(R.string.change) { _, _ -> chooseMode(firstRun = false) }
                        .show()
                }
            }
        }.start()
    }

    /** The status screen: with no [retry], a spinner (starting, connecting);
     *  with one, Try again and Change buttons (could not reach Sieve). */
    private fun showStatus(text: String, retry: (() -> Unit)? = null) {
        status.text = text
        spinner.visibility = if (retry == null) View.VISIBLE else View.GONE
        statusActions.visibility = if (retry == null) View.GONE else View.VISIBLE
        retryButton.setOnClickListener { retry?.invoke() }
        refresh.isRefreshing = false
        statusPanel.visibility = View.VISIBLE
    }

    private fun hideStatus() {
        statusPanel.visibility = View.GONE
    }

    private fun buildStatusPanel(): View {
        status = TextView(this).apply {
            gravity = Gravity.CENTER
            setTextAppearance(com.google.android.material.R.style.TextAppearance_Material3_BodyLarge)
            setTextColor(getColor(R.color.ink_2))
        }
        spinner = CircularProgressIndicator(this).apply {
            isIndeterminate = true
            indicatorSize = dp(36)
            trackThickness = dp(3)
            setIndicatorColor(getColor(R.color.indigo))
            visibility = View.GONE
        }
        retryButton = MaterialButton(this).apply { setText(R.string.retry) }
        val changeButton = MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle).apply {
            setText(R.string.change)
            setOnClickListener { chooseMode(firstRun = false) }
        }
        statusActions = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            visibility = View.GONE
            addView(retryButton)
            addView(changeButton, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { marginStart = dp(12) })
        }
        val wrap = { bottom: Int ->
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                .apply { bottomMargin = dp(bottom) }
        }
        statusPanel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(getColor(R.color.paper))
            setPadding(dp(32), dp(32), dp(32), dp(32))
            // Takes the touches, so the page underneath is not scrolled or tapped.
            isClickable = true
            addView(ImageView(this@MainActivity).apply {
                setImageResource(R.drawable.ic_launcher)
                importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            }, LinearLayout.LayoutParams(dp(72), dp(72)).apply { bottomMargin = dp(28) })
            addView(spinner, wrap(20))
            addView(status, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = dp(20) })
            addView(statusActions, wrap(0))
        }
        return statusPanel
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    // ------------------------------------------------------------ webview

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            userAgentString = "$userAgentString SieveAndroid/0.6"
        }
        refresh.setOnChildScrollUpCallback { _, _ -> web.scrollY > 0 }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                // Sieve's own pages stay here; everything else (YouTube, a
                // provider) opens in its own app or the browser.
                if (url.host == Uri.parse(baseUrl).host && url.port == Uri.parse(baseUrl).port) return false
                return try {
                    startActivity(Intent(Intent.ACTION_VIEW, url)); true
                } catch (e: Exception) { false }
            }

            override fun onPageFinished(view: WebView, url: String) {
                refresh.isRefreshing = false
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    showStatus(getString(R.string.unreachable, baseUrl)) { hideStatus(); web.reload() }
                }
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                fullscreenView = view
                fullscreenCallback = callback
                root.addView(view, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                refresh.visibility = View.GONE
            }

            override fun onHideCustomView() = exitFullscreen()

            override fun onShowFileChooser(
                view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams
            ): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try {
                    pickFile.launch("*/*"); true
                } catch (e: Exception) {
                    fileCallback = null; false
                }
            }
        }
        // Exports (notes, backups) and saved videos: hand them to Android's downloader.
        web.setDownloadListener { url, _, disposition, mime, _ ->
            try {
                val name = URLUtil.guessFileName(url, disposition, mime)
                val request = DownloadManager.Request(Uri.parse(url))
                    .setTitle(name)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
                (getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(request)
                Toast.makeText(this, getString(R.string.downloading, name), Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                Toast.makeText(this, e.message ?: "download failed", Toast.LENGTH_LONG).show()
            }
        }
    }

    private fun exitFullscreen() {
        fullscreenView?.let { root.removeView(it) }
        fullscreenView = null
        fullscreenCallback?.onCustomViewHidden()
        fullscreenCallback = null
        refresh.visibility = View.VISIBLE
    }

    // --------------------------------------------------------------- menu

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean = when (item.itemId) {
        R.id.action_reload -> { web.reload(); true }
        R.id.action_mode -> { chooseMode(firstRun = false); true }
        else -> super.onOptionsItemSelected(item)
    }

    // -------------------------------------------------------------- share

    private fun sharedUrl(intent: Intent?): String? {
        if (intent?.action != Intent.ACTION_SEND) return null
        val text = intent.getStringExtra(Intent.EXTRA_TEXT) ?: return null
        return Regex("https?://\\S+").find(text)?.value
    }

    private fun siftUrl(link: String) = "$baseUrl/sift?q=${URLEncoder.encode(link, "UTF-8")}&run=1"
}
