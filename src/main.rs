//! Entry point: `hoi4-translator --src MOD --dst OUT --target ru`.

use clap::Parser;

use hoi4_translator::cli::{run, Cli};
use hoi4_translator::config::init_logging;

fn main() -> std::process::ExitCode {
    init_logging();
    let cli = Cli::parse();
    let code = run(cli);
    std::process::ExitCode::from(code as u8)
}
