git config user.email "Rityxtech@users.noreply.github.com"
git config user.name "Rityxtech"
git checkout --orphan pristine
git add -A
git commit --author="Rityxtech <Rityxtech@users.noreply.github.com>" -m "Initial Commit: TeleBoosterPro"
git branch -D main
git branch -m main
git push -f origin main
