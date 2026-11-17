A real Tauri 2 app, as small as one can be, for test_desktop.TauriOnWindowsTest (FACTORY_TEST_TAURI=1): one command
that answers, one that fails, an event, the global API (`withGlobalTauri`) instead of a bundler, and `custom-protocol`
on by default so `cargo build` embeds the page the way a release does. The test writes its icon (a 32x32 .ico) and
builds it on Windows through fx, then runs check.js on it.
