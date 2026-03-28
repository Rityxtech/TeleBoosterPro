git init
git rm -r --cached .env *.session venv __pycache__ processed_*.txt *.csv node_modules 2>$null
git add .
git commit -m "Secure initial commit: Rebrand to TeleBoosterPro"
git branch -M main
git remote remove origin 2>$null
git remote add origin https://github.com/Rityxtech/TeleBoosterPro.git
git push -u origin main
