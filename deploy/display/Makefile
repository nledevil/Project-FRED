# Native animations for the chest panel.
#
# The chest Pi has no compiler (DietPi minimal, and it does not need one), so
# build on the head Pi — same aarch64 Cortex-A72 — and copy the binary over:
#
#     make && make install-chest
#
# -O3 measured ~10% faster than -O2 here and stays bit-identical to the Python
# renderer; neither enables -ffast-math, so float results are unchanged.

CC      ?= gcc
CFLAGS  ?= -O3 -mcpu=cortex-a72 -Wall -Wextra
LDLIBS  := -lm

CHEST   ?= 10.0.0.11
DEST    ?= /home/dietpi/display

BINS    := voice_hud

.PHONY: all clean install-chest verify

all: $(BINS)

voice_hud: voice_hud.c theme_colors.h
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

# The palettes, generated from theme.py so the two renderers cannot disagree
# about a colour. Regenerated whenever the themes change; it is a build
# artifact, not source, and is git-ignored like the binary.
theme_colors.h: theme.py tools/gen_theme_colors.py
	python3 tools/gen_theme_colors.py > $@

# Ship it, then bounce the animation so the daemon picks up the new binary.
install-chest: $(BINS)
	scp -q $(BINS) $(CHEST):$(DEST)/
	ssh $(CHEST) 'chmod +x $(DEST)/voice_hud'
	@echo "installed on $(CHEST); switch to it with:"
	@echo "  curl -sX POST -H 'Content-Type: application/json' \\"
	@echo "       -d '{\"animation\":\"voice-hud-c\"}' http://$(CHEST):8081/api/animation"

# Prove the native renderer still matches voice_hud.py pixel for pixel.
verify: $(BINS)
	python3 tools/verify_voice_hud.py

clean:
	rm -f $(BINS) theme_colors.h
