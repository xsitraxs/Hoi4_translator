//! `hoi4-translator-gui` — the window, as its own program.
//!
//! The console binary and the window share the whole library; this one only
//! has flags for the window itself, so `hoi4-translator --help` stays centred
//! on translating. Everything else (settings, the log, the cache) is the same.

use std::process::ExitCode;

use clap::Parser;

#[derive(Debug, Parser)]
#[command(
    name = "hoi4-translator-gui",
    version = hoi4_translator::__version__,
    about = "Окно HoI4 Localization Tool: тот же перевод, но в браузере на этом компьютере",
    long_about = "Запускает локальный сервер и печатает адрес с ключом доступа.\n\
                  Откройте этот адрес в браузере — это и есть окно программы.\n\
                  Сервер слушает только этот компьютер, если не указать --host."
)]
struct Args {
    /// Адрес для прослушивания; 0.0.0.0 открывает окно всей сети.
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    /// Порт; если занят, берётся свободный.
    #[arg(long, default_value_t = 8765)]
    port: u16,
    /// Открыть браузер сразу.
    #[arg(long)]
    open: bool,
}

fn main() -> ExitCode {
    let args = Args::parse();
    // The same log file as the console run: a window that is closed must not
    // lose the record of what happened.
    hoi4_translator::config::init_logging();
    match hoi4_translator::gui::serve(&args.host, args.port, args.open) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("Не удалось открыть окно: {error:#}");
            ExitCode::from(1)
        }
    }
}
