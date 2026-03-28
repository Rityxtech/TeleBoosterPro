git config user.email "Rityxtech@users.noreply.github.com"
git config user.name "Rityxtech"
git commit -m "Secure initial commit: Rebrand to TeleBoosterPro"
git branch -M main
git remote remove origin 2>$null
git remote add origin https://github.com/Rityxtech/TeleBoosterPro.git
git push -u origin main
