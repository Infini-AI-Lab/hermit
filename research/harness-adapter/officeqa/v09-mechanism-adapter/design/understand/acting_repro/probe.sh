echo "PATH=$PATH"
echo "--- which python/python3/rg"
whence -a python python3 rg 2>&1
echo "--- python3 --version"
python3 --version 2>&1 | head -1
echo "--- python numpy"
python -c 'import numpy; print(numpy.__version__)' 2>&1 | tail -1
echo "--- rg"
rg --version 2>&1 | head -1
echo "--- heredoc"
cat <<'X'
heredoc-ok
X
echo "--- TMPPREFIX=${TMPPREFIX:-<unset>} TMPDIR=$TMPDIR"
echo "--- ls .."
ls .. 2>&1 | head -2
