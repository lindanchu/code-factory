#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Where the app's API is, as fx set it when this was built.
#[tauri::command]
fn api_url() -> String {
    option_env!("FACTORY_API_URL").or(option_env!("BASE_URL")).unwrap_or("").trim_end_matches('/').to_string()
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![api_url])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
