# VLC plugin for .vh format
# Requires: libvlccore-dev, libvlc-dev, libsqlite3-dev

PLUGIN_NAME = libvh_plugin

VLC_INCLUDE = /usr/include/vlc/plugins
VLC_CFLAGS  = -I/usr/include -I$(VLC_INCLUDE) -DMODULE_STRING=\"vh\" -DMODULE_NAME=vh -D__PLUGIN__
SQLITE_LIBS = -lsqlite3

CC = gcc
CFLAGS = -Wall -O2 -fPIC -shared $(VLC_CFLAGS) $(shell pkg-config --cflags libvlc 2>/dev/null)
LDFLAGS = $(SQLITE_LIBS) -lvlccore

# VLC plugin directory
VLC_PLUGIN_DIR = $(shell pkg-config --variable=pluginsdir vlc 2>/dev/null || echo /usr/lib/x86_64-linux-gnu/vlc/plugins)
USER_PLUGIN_DIR = $(HOME)/.local/lib/vlc/plugins/demux

SRC = vh_demux.c
OUT = $(PLUGIN_NAME).so

.PHONY: all clean install install-user

all: $(OUT)

$(OUT): $(SRC)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(OUT)

install: $(OUT)
	install -d $(VLC_PLUGIN_DIR)/demux
	install -m 644 $(OUT) $(VLC_PLUGIN_DIR)/demux/

install-user: $(OUT)
	install -d $(USER_PLUGIN_DIR)
	install -m 644 $(OUT) $(USER_PLUGIN_DIR)/
	@echo "Installed to $(USER_PLUGIN_DIR)/$(OUT)"
	@echo "Restart VLC to load the plugin."
