echo "PATH=$PATH"
echo "--- whence"
whence -a python python3 rg 2>&1
echo "--- python3"
python3 -c 'import sys, numpy, pandas, sqlite3; print(sys.version.split()[0], sys.prefix, numpy.__version__, pandas.__version__, sqlite3.sqlite_version)' 2>&1 | tail -1
echo "--- rg on corpus"
rg -c 'Treasury' /Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/officeqa-pro133-none-v8-work/prepared/agent/corpus/treasury_bulletin_1939_01.txt 2>&1
echo "--- heredoc"
python3 - <<'PY'
print('heredoc-ok')
PY
echo "--- venv still denied?"
ls /Users/albertzhao/.codex/.chatgpt-projects/g-p-6aa867f0012881918044d250b055e9dd/adapter-comparison-work/.venv 2>&1 | head -1
echo "--- homebrew denied?"
ls /opt/homebrew/bin 2>&1 | head -1
