$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$PROJECT_DIR = Split-Path -Parent $SCRIPT_DIR

Set-Location -LiteralPath $PROJECT_DIR
python .\WEBUI_APP.py
