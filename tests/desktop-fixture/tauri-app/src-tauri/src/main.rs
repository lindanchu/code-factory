#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[tauri::command]
fn greet(name: &str) -> String { format!("Hello, {}!", name) }

#[tauri::command]
fn fails() -> Result<(), String> { Err("disk full".into()) }

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![greet, fails])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
