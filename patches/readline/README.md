Official GNU readline patches (`readlineXY-NNN`), applied in order by the
readline stage of the Dockerfile. `scripts/update-deps.py` downloads them from
https://ftp.gnu.org/gnu/readline/readline-X.Y-patches/, checks each one's GPG
signature against the readline key pinned in keys/, and removes patches for
other versions.
Don't edit them by hand.

These patches are part of GNU readline: Copyright (C) Free Software
Foundation, Inc., licensed under the GNU General Public License, version 3
or later (see readline's COPYING). The image also ships them, in
/usr/local/share/licenses/readline/patches/, as part of readline's
Corresponding Source.
