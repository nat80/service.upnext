#!/bin/bash
# Build release: generates zip, addons.xml and addons.xml.md5

set -e

ADDON_XML="addon.xml"
NAME=$(grep 'addon id=' "$ADDON_XML" | sed -nE 's/.*id="([^"]+)".*/\1/p' | head -1)
VERSION=$(grep 'addon id=' "$ADDON_XML" | sed -nE 's/.*version="([^"]+)".*/\1/p' | head -1)

echo "Building $NAME version $VERSION"
mkdir -p repo/service.upnext

# Create temp copy
mkdir -p .build_temp/service.upnext
cp addon.xml .build_temp/service.upnext/
cp -r resources .build_temp/service.upnext/
cp LICENSE README.md changelog.txt .build_temp/service.upnext/

cd .build_temp

# Create zip
ZIP_FILE="../repo/service.upnext/${NAME}-${VERSION}.zip"
zip -r "$ZIP_FILE" service.upnext/ \
    -x "*/\.*" "*/\__pycache__/*" "*/*.pyc" "*/*.pyo" "*.DS_Store" \
    > /dev/null 2>&1

echo "Created: $(basename $ZIP_FILE)"

cd ..
rm -rf .build_temp

# Generate addons.xml and md5
echo "Generating addons.xml and addons.xml.md5"

python3 - <<'PYEOF'
import hashlib
from pathlib import Path

ADDON_XML = Path("addon.xml")
REPO_DIR = Path("repo")
addons_xml = REPO_DIR / "addons.xml"

# Read addon.xml and extract addon element
with open(ADDON_XML) as f:
    content = f.read().strip()
    # Remove XML declaration if present
    if content.startswith('<?xml'):
        content = content.split('?>', 1)[1].strip()

wrapped = '<?xml version="1.0" encoding="UTF-8"?>\n<addons>\n' + content + '\n</addons>\n'

# Write addons.xml
with open(addons_xml, 'w') as f:
    f.write(wrapped)

# Write md5
md5 = hashlib.md5(wrapped.encode()).hexdigest()
with open(REPO_DIR / "addons.xml.md5", 'w') as f:
    f.write(md5)

print("  Created: addons.xml")
print("  Created: addons.xml.md5")
PYEOF

echo ""
echo "Done! Files in repo/:"
ls -1 repo/
