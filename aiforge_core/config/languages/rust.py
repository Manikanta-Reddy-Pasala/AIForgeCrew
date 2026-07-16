"""Rust language profile."""
from __future__ import annotations

from aiforge_core.config.languages.base import LanguageProfile

# Commands copied verbatim from repo_standards._DEFAULTS_BY_LANG["rust"].
# syntax_check is None: Rust has no cheap non-executing checker wired into
# syntax_guard (cargo check compiles the crate), so it falls back to the
# brace-balance heuristic pre-write and is typechecked at real build time.
PROFILE = LanguageProfile(
    name="rust",
    aliases=("rs", "cargo"),
    extensions=(".rs",),
    build_markers=("Cargo.toml",),
    compile_cmd="cargo check",
    test_cmd="cargo test",
    lint_cmd="cargo clippy -- -D warnings",
    format_cmd="cargo fmt",
    syntax_check=None,
    toolchain_candidates={"cargo": ("cargo",), "rustc": ("rustc",)},
    install_hint="Install the Rust toolchain via rustup (cargo, rustc).",
    conventions=(
        "edition in Cargo.toml; no unwrap()/expect() in library code — return "
        "Result and use ?; run `cargo test`; lint with clippy (-D warnings); "
        "format with `cargo fmt`; prefer borrowing over cloning."
    ),
)

__all__ = ["PROFILE"]
