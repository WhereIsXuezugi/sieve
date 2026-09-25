plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "app.sieve"
    compileSdk = 34

    defaultConfig {
        applicationId = "app.sieve"
        minSdk = 24
        targetSdk = 34
        versionCode = 9
        versionName = "0.7.0"
        // Phones (arm64) and the emulator (x86_64). Add "armeabi-v7a" for old 32-bit phones.
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Signed with the debug key so the CI build installs directly; sign with
            // your own key for anything you distribute (see android/README.md).
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

// The app runs the same Python package as the desktop server: copy it in at
// build time rather than keeping a second copy in the repository.
val syncSieve by tasks.registering(Sync::class) {
    from("../../sieve") {
        exclude("**/__pycache__/**")
    }
    into(layout.buildDirectory.dir("generated/sieve-python/sieve"))
}
tasks.named("preBuild") { dependsOn(syncSieve) }
// Chaquopy's merge<Variant>PythonSources tasks read syncSieve's output but don't
// depend on preBuild, so wire them up explicitly (covers debug and release).
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }
    .configureEach { dependsOn(syncSieve) }

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            install("-r", "requirements-android.txt")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
            srcDir(layout.buildDirectory.dir("generated/sieve-python").get().asFile.path)
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.1")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
}
