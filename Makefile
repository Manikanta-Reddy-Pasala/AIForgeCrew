.PHONY: help install test test-docker ui clean installers installer-payload installer-deb installer-dmg installer-msi installer-verify

help:
	@echo "Dev targets:"
	@echo "  install   .venv + uv pip install -e .[dev]"
	@echo "  test      pytest tests/python"
	@echo "  test-docker  the CI run, in a throwaway container (fresh clone + uv.lock)"
	@echo "  ui        vite build (web/dist)"
	@echo "  installers   .dmg + .msi + .deb into dist/installer (skips what it cannot build)"
	@echo "  installer-verify  build the .deb AND install+serve it in a container"
	@echo "  clean     remove caches + build artifacts"
	@echo ""
	@echo "Run the full stack with: docker compose up -d --build  (see QUICKSTART.md)"

install:
	uv venv .venv
	# --all-extras, matching CI: tests require the declared extras (chonkie et
	# al) instead of skipping when they are missing, so a dev venv that lacks
	# them would go red on tests CI runs green.
	.venv/bin/uv pip install -e ".[dev,xlsx,structured,crawl,chunking,embed-static]"

test:
	.venv/bin/pytest tests/python -v

test-docker:
	# Fresh environment: clean clone of HEAD, uv sync --frozen, plain pytest.
	# Extra args: make test-docker ARGS="-m live_tmux"
	scripts/test_in_docker.sh $(ARGS)

# ── native installers (see installer/README.md) ───────────────────────
# The payload (wheel + uv per target) is built once; each package is a thin
# wrapper around it. Targets whose toolchain is missing are SKIPPED loudly
# rather than failing the run: a mac can build the .dmg and the .msi (wixl) but
# not the .deb, and a Linux box is the other way round.
installer-payload:
	installer/build_payload.sh

installer-deb: installer-payload
	@command -v dpkg-deb >/dev/null 2>&1 \
	  && installer/linux/build-deb.sh \
	  || echo "skip .deb — no dpkg-deb (build it in a container: make installer-verify)"

installer-dmg: installer-payload
	@command -v hdiutil >/dev/null 2>&1 \
	  && installer/macos/build-dmg.sh \
	  || echo "skip .dmg — hdiutil is macOS-only"

installer-msi: installer-payload
	@command -v wixl >/dev/null 2>&1 \
	  && installer/windows/build-msi.sh \
	  || echo "skip .msi — no wixl (brew install msitools / apt-get install wixl)"

installers: installer-deb installer-dmg installer-msi
	@ls -lh dist/installer/*.deb dist/installer/*.dmg dist/installer/*.msi 2>/dev/null || true

# The only test that proves a package works: install it on a clean OS and hit
# the API it serves.
installer-verify:
	installer/verify-deb.sh

ui:
	cd web && npm install && npm run build

clean:
	rm -rf .pytest_cache .ruff_cache __pycache__ build dist *.egg-info
