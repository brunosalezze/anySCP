#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Run anySCP straight from the source tree.
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m anyscp "$@"
