"""Builds src/api/index.html from ui/index.src.html.

The three typefaces are embedded in the page as base64, so the bank makes no
requests to any third-party font host. Run after editing index.src.html:

    python ui/build.py
"""
import base64
import pathlib

here = pathlib.Path(__file__).parent
FACES = [
    ("Instrument Serif", "normal", "400", "instrument-serif-400.woff2"),
    ("Instrument Serif", "italic", "400", "instrument-serif-400-italic.woff2"),
    ("Geist", "normal", "100 900", "geist-variable.woff2"),
    ("Geist Mono", "normal", "100 900", "geist-mono-variable.woff2"),
]
css = ""
for family, style, weight, name in FACES:
    data = base64.b64encode((here / "fonts" / name).read_bytes()).decode()
    css += (f'@font-face{{font-family:"{family}";font-style:{style};font-weight:{weight};font-display:swap;'
            f'src:url(data:font/woff2;base64,{data}) format("woff2")}}\n')

src = (here / "index.src.html").read_text(encoding="utf-8")
assert "/*__FONTS__*/" in src
out = here.parent / "src" / "api" / "index.html"
out.write_text(src.replace("/*__FONTS__*/", css), encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
