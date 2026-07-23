# Сборка standalone-приложения: dist\JitsiStreamLogger\JitsiStreamLogger.exe
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Результат — папка dist\JitsiStreamLogger (можно перенести куда угодно ЦЕЛИКОМ):
#   JitsiStreamLogger.exe        — двойной клик = запустить сейчас (иконка в трее)
#   Включить автозапуск.bat      — тихий запуск при каждом входе в Windows
#   Выключить автозапуск.bat     — убрать автозапуск
#   extension\                   — расширение для Chrome (Load unpacked)
# FFmpeg по-прежнему нужен в PATH (winget install Gyan.FFmpeg).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Не найден $py — сначала создайте venv (см. README)" }

Write-Host "[1/4] Устанавливаю PyInstaller..."
& $py -m pip install --quiet --disable-pip-version-check pyinstaller

Write-Host "[2/4] Генерирую иконку..."
New-Item -ItemType Directory -Force (Join-Path $root "assets") | Out-Null
Push-Location $root
& $py -c "from PIL import Image; from app.tray import _icon_image; _icon_image(False).resize((256,256), Image.LANCZOS).save(r'assets\icon.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])"

Write-Host "[3/4] Собираю exe (PyInstaller)..."
& $py -m PyInstaller --noconfirm --clean --onedir --noconsole `
    --name JitsiStreamLogger `
    --icon "$root\assets\icon.ico" `
    --hidden-import pystray._win32 `
    --collect-all windows_capture `
    "$root\launcher.py"
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { throw "PyInstaller завершился с ошибкой ($code)" }

Write-Host "[4/4] Складываю комплект рядом с exe..."
$dist = Join-Path $root "dist\JitsiStreamLogger"
Copy-Item -Recurse -Force (Join-Path $root "extension") (Join-Path $dist "extension")
Set-Content -Encoding ascii (Join-Path $dist "Включить автозапуск.bat") "@echo off`r`nstart `"`" `"%~dp0JitsiStreamLogger.exe`" --install-autostart"
Set-Content -Encoding ascii (Join-Path $dist "Выключить автозапуск.bat") "@echo off`r`nstart `"`" `"%~dp0JitsiStreamLogger.exe`" --remove-autostart"

Write-Host ""
Write-Host "Готово: $dist"
Write-Host "  JitsiStreamLogger.exe — запустить сейчас; батники — автозапуск вкл/выкл."
