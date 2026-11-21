A desktop app with an API of its own, for test_isolation.RealDesktopReviewTest (FX_SANDBOX_REAL=1 and
FACTORY_TEST_TAURI=1): a merge request's API and website run in its review's box, and its real Tauri build runs on
Windows and calls that API across the sandbox. `api/server.js` serves the API and the page; the page asks the API for a
greeting, at its own origin as a website and at the address the build was pointed at (`FACTORY_API_URL`, else
`BASE_URL`, read by `api_url` when Rust compiles) as the real app. The test writes its icon, as for tauri-app.
