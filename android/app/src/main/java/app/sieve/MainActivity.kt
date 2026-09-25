package app.sieve

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
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
        status = TextView(this).apply {
            setPadding(48, 48, 48, 48)
            textSize = 16f
        }
        web = WebView(this)
        refresh = SwipeRefreshLayout(this).apply { addView(web) }
        refresh.setOnRefreshListener { web.reload() }
        root.addView(refresh)
        root.addView(status)
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
        AlertDialog.Builder(this)
            .setTitle(R.string.choose_title)
            .setMessage(R.string.choose_message)
            .setCancelable(!firstRun)
            .setPositiveButton(R.string.mode_local) { _, _ ->
                prefs.edit().putString("mode", "local").apply()
                openLocal()
            }
            .setNegativeButton(R.string.mode_remote) { _, _ -> askServer() }
            .show()
    }

    private fun askServer() {
        val input = EditText(this).apply {
            hint = "http://192.168.1.20:8377"
            setText(prefs.getString("remote", ""))
            setSingleLine()
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.server_title)
            .setMessage(R.string.server_message)
            .setView(input)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                val url = input.text.toString().trim().trimEnd('/')
                    .let { if (it.startsWith("http")) it else "http://$it" }
                prefs.edit().putString("mode", "remote").putString("remote", url).apply()
                openRemote(url)
            }
            .setNegativeButton(android.R.string.cancel) { _, _ ->
                if (prefs.getString("mode", null) == null) chooseMode(firstRun = true)
            }
            .show()
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
                    status.visibility = View.GONE
                    web.loadUrl(pendingShare?.let { siftUrl(it) } ?: url)
                    pendingShare = null
                } else {
                    showStatus(getString(R.string.unreachable, url))
                    AlertDialog.Builder(this)
                        .setMessage(getString(R.string.unreachable, url))
                        .setPositiveButton(R.string.retry) { _, _ -> waitThenLoad(url, seconds) }
                        .setNegativeButton(R.string.change) { _, _ -> chooseMode(firstRun = false) }
                        .show()
                }
            }
        }.start()
    }

    private fun showStatus(text: String) {
        status.text = text
        status.visibility = View.VISIBLE
    }

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
                if (request.isForMainFrame) showStatus(getString(R.string.unreachable, baseUrl))
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
