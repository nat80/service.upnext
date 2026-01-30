#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEV_PATH="$SCRIPT_DIR"
KODI_ADDON_PATH="$HOME/Library/Application Support/Kodi/addons/service.upnext"

echo -e "${YELLOW}Setting up symlink for Kodi addon...${NC}"
echo -e "${YELLOW}Script directory: $SCRIPT_DIR${NC}"

if [ ! -d "$DEV_PATH" ]; then
    echo -e "${RED}Error: Dev folder not found at $DEV_PATH${NC}"
    exit 1
fi

if [ ! -d "$HOME/Library/Application Support/Kodi" ]; then
    echo -e "${RED}Error: Kodi directory not found at $HOME/Library/Application Support/Kodi${NC}"
    exit 1
fi

if [ -L "$KODI_ADDON_PATH" ]; then
    echo -e "${YELLOW}Removing existing symlink at $KODI_ADDON_PATH${NC}"
    rm "$KODI_ADDON_PATH"
elif [ -d "$KODI_ADDON_PATH" ]; then
    echo -e "${YELLOW}Removing existing directory at $KODI_ADDON_PATH${NC}"
    rm -rf "$KODI_ADDON_PATH"
fi

echo -e "${YELLOW}Creating symlink...${NC}"
ln -s "$DEV_PATH" "$KODI_ADDON_PATH"

if [ -L "$KODI_ADDON_PATH" ]; then
    echo -e "${GREEN}✓ Symlink created successfully!${NC}"
    echo -e "${GREEN}Dev: $DEV_PATH${NC}"
    echo -e "${GREEN}Kodi: $KODI_ADDON_PATH${NC}"
else
    echo -e "${RED}✗ Failed to create symlink${NC}"
    exit 1
fi