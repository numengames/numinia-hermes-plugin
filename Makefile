# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

# `make check` runs every gate (see scripts/check.sh for the environment variables it honours).
.PHONY: check fmt integration
check:
	./scripts/check.sh
fmt:
	$${PY:-python3} -m ruff format . && $${PY:-python3} -m ruff check --fix .

# End-to-end run with the real Hermes and engine in a throwaway home. The harness lives OUTSIDE the shipped tree
# (sibling folder plugin-hermes-3d-dev, or DEV=/path) because it imports Hermes internals.
DEV ?= $(abspath ../plugin-hermes-3d-dev)
.PHONY: integration
integration:
	HERMES_3D_PLUGIN=$(CURDIR) $(DEV)/tools/integration/run_integration.sh
