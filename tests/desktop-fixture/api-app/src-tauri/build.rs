fn main() {
    // the address fx points the build at: another review's API is another build
    println!("cargo:rerun-if-env-changed=FACTORY_API_URL");
    println!("cargo:rerun-if-env-changed=BASE_URL");
    tauri_build::build()
}
