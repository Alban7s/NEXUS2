"""Generate the ZIP fixtures used by the tests into tests/fixtures/_generated/.

Valid: static-site.zip, react-vite-app.zip (a wrapping folder, like a real upload)
Invalid: corrupt, empty-file, empty-zip, no-project, path-traversal, absolute-path,
         symlink, env-file, bomb, windows-reserved, duplicate, nested-zip
"""
from __future__ import annotations

import stat
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "_generated"


def zip_dir(src: Path, dest: Path, wrap: str | None = None) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file() and "node_modules" not in p.parts:
                arc = p.relative_to(src).as_posix()
                zf.write(p, f"{wrap}/{arc}" if wrap else arc)


def build() -> dict[str, Path]:
    OUT.mkdir(exist_ok=True)
    out: dict[str, Path] = {}

    out["static"] = OUT / "static-site.zip"
    zip_dir(HERE / "static-site", out["static"])
    out["react"] = OUT / "react-vite-app.zip"
    zip_dir(HERE / "react-vite-app", out["react"], wrap="react-vite-app")

    out["corrupt"] = OUT / "corrupt.zip"
    out["corrupt"].write_bytes(b"PK\x03\x04" + b"this is not really a zip archive" * 20)
    out["empty_file"] = OUT / "empty-file.zip"
    out["empty_file"].write_bytes(b"")
    out["empty_zip"] = OUT / "empty-zip.zip"
    zipfile.ZipFile(out["empty_zip"], "w").close()

    def make(name: str, writer) -> Path:
        path = OUT / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            writer(zf)
        return path

    out["no_project"] = make("no-project.zip", lambda z: z.writestr("readme.txt", "no site here"))
    out["traversal"] = make("path-traversal.zip", lambda z: (z.writestr("index.html", "<h1>x</h1>"),
                                                            z.writestr("../../evil.txt", "pwned")))
    out["absolute"] = make("absolute-path.zip", lambda z: (z.writestr("index.html", "x"),
                                                          z.writestr("/etc/cron.d/evil", "x")))
    out["env_file"] = make("env-file.zip", lambda z: (z.writestr("index.html", "x"),
                                                      z.writestr(".env", "SECRET=1")))
    out["nested_zip"] = make("nested-zip.zip", lambda z: (z.writestr("index.html", "x"),
                                                          z.writestr("inner.zip", "PK")))
    out["reserved"] = make("windows-reserved.zip", lambda z: (z.writestr("index.html", "x"),
                                                              z.writestr("assets/CON.txt", "x")))

    def symlink(z: zipfile.ZipFile) -> None:
        z.writestr("index.html", "x")
        info = zipfile.ZipInfo("link-to-passwd")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    out["symlink"] = make("symlink.zip", symlink)

    def duplicate(z: zipfile.ZipFile) -> None:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            z.writestr("index.html", "one")
            z.writestr("INDEX.html", "two")
    out["duplicate"] = make("duplicate.zip", duplicate)

    # ZIP bomb stand-in: 3 MB of zeros compresses ~1000:1.
    out["bomb"] = make("bomb.zip", lambda z: (z.writestr("index.html", "x"),
                                              z.writestr("big.bin", b"\0" * (3 * 1024 * 1024))))
    return out


if __name__ == "__main__":
    for k, v in build().items():
        print(f"{k:12s} {v.name:24s} {v.stat().st_size:>8d} bytes")
