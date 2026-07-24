"""Собирает extension/manifest.json из шаблона и доменов из config.toml.

Домены рабочего Jitsi-сервера в репозиторий не попадают: в git лежит
manifest.template.json с плейсхолдером «__DOMAIN_MATCHES__», а готовый
manifest.json генерируется локально и заигнорен.

    python tools/make_manifest.py                  # домены из config.toml
    python tools/make_manifest.py meet.jit.si      # домены явным списком
    python tools/make_manifest.py --check          # только проверить (для CI)

После генерации нажмите «Обновить» на карточке расширения в chrome://extensions
и перезагрузите вкладку созвона — content-скрипты внедряются при загрузке
страницы.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "extension" / "manifest.template.json"
TARGET = ROOT / "extension" / "manifest.json"
PLACEHOLDER = "__DOMAIN_MATCHES__"

sys.path.insert(0, str(ROOT))
from app.config import appdata_dir  # noqa: E402


def domains_from_config() -> list[str]:
    cfg_path = appdata_dir() / "config.toml"
    if not cfg_path.exists():
        raise SystemExit(
            f"Нет {cfg_path} — запустите приложение один раз или укажите "
            f"домены аргументами: python tools/make_manifest.py example.com")
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    domains = [str(d).strip().lower()
               for d in raw.get("general", {}).get("allowed_domains", [])
               if str(d).strip()]
    if not domains:
        raise SystemExit(f"В {cfg_path} пустой allowed_domains — нечего "
                         f"подставлять в matches")
    return domains


def render(domains: list[str]) -> dict:
    manifest = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    matches = [f"https://{d}/*" for d in domains]
    patched = 0
    for entry in manifest.get("content_scripts", []):
        if PLACEHOLDER in entry.get("matches", []):
            entry["matches"] = list(matches)
            patched += 1
    if not patched:
        raise SystemExit(f"В {TEMPLATE.name} не найден плейсхолдер "
                         f"{PLACEHOLDER} — шаблон испорчен")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("domains", nargs="*",
                    help="домены Jitsi; по умолчанию — allowed_domains "
                         "из config.toml")
    ap.add_argument("--check", action="store_true",
                    help="не писать файл, а проверить, что manifest.json "
                         "совпадает с ожидаемым (код 1, если нет)")
    args = ap.parse_args()

    domains = [d.strip().lower() for d in args.domains if d.strip()]
    if not domains:
        domains = domains_from_config()
    manifest = render(domains)
    text = json.dumps(manifest, ensure_ascii=False, indent=4) + "\n"

    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            raise SystemExit(
                f"{TARGET.name} не соответствует шаблону и доменам "
                f"{', '.join(domains)} — перегенерируйте его: "
                f"python tools/make_manifest.py")
        print(f"{TARGET.name} актуален: {', '.join(domains)}")
        return

    TARGET.write_text(text, encoding="utf-8")
    print(f"Записан {TARGET}")
    print(f"Домены в matches: {', '.join(domains)}")
    print("Теперь: chrome://extensions → «Обновить» на карточке расширения, "
          "затем F5 на вкладке созвона.")


if __name__ == "__main__":
    main()
