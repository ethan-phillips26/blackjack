"""
pygame rendering, animation, and keyboard input.

This is the ONLY file that imports pygame. It draws the game state it is handed
and pushes keystrokes into the hardware backend; it holds no game or money
state of its own.

Everything here is built for composite video on a 480-line CRT: chunky fonts,
fat strokes, no 1px detail, and every pixel that matters kept inside the
overscan-safe rectangle.

ANIMATION
---------
All motion lives here and is PURELY COSMETIC -- it is layered on top of the
rules engine, never inside it. game.py still deals all four opening cards in
one call; this file remembers which of them it has already shown and walks
them out of the shoe in real table order (player, dealer up-card, player,
dealer hole card). Nothing an animation does can change a card, a bet, or a
quarter, and `config.ANIMATIONS_ENABLED = False` collapses every duration to
zero and leaves the same fully playable machine.

The one place motion touches pacing is main.py's `ui.is_dealing()` check,
which holds the dealer's next card until the previous one has landed. Money is
credited the instant the hand settles regardless of what is still moving.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass

import pygame

import anim
import audio
import config
from anim import RollingCounter, Tween
from cards import Card
from game import BlackjackGame, Phase, hand_value


# Vertical layout, top to bottom. Kept here rather than in config.py because
# these are pixel positions internal to this renderer, not machine settings.
# The whole stack has to fit between SAFE_RECT's top (24) and bottom (456), and
# the banner font is 56px tall, so there is not much slack -- check the result
# banner still clears the player's cards if you move anything.
ROW_DEALER_LABEL = 64
ROW_DEALER_CARDS = 86
ROW_PLAYER_LABEL = 190
ROW_PLAYER_CARDS = 212
ROW_BANNER = 326
ROW_PROMPT = 396
ROW_HINTS = 432

# Attract-screen layout.
ATTRACT_TITLE_TOP = 62
ATTRACT_CARDS_Y = 200  # centre line of the two demo cards
ATTRACT_INSERT_TOP = 286
ATTRACT_START_TOP = 336
ATTRACT_MARQUEE_TOP = 424

# The player is dealt first at a real table, so when several cards appear in
# one tick the player's outrank the dealer's at the same position.
DEAL_PRIORITY = {"player": 0, "dealer": 1}


#: name -> Sound, filled by init_audio(). Empty means "running silent", which
#: is a perfectly good state for this machine to be in.
_sounds: dict[str, "pygame.mixer.Sound"] = {}


#: SDL video drivers that render correctly and show the result to nobody.
#: "dummy" throws the pixels away; "offscreen" keeps them in a buffer. Either
#: way the game runs perfectly and the television never changes, which is a
#: uniquely frustrating way for a cabinet to fail.
INVISIBLE_DRIVERS = frozenset({"dummy", "offscreen"})


def diagnose_display() -> list[str]:
    """Work out WHY SDL could not open a real display, and say so concretely.

    SDL falls back through its drivers silently and lands on an invisible one
    only when every real option failed. It never reports which, so this checks
    the handful of things that actually go wrong on a Pi cabinet.
    """
    reasons: list[str] = []

    # Checked first because it causes all the others at once, and because the
    # reason people reach for sudo -- GPIO access -- has not been necessary for
    # years. sudo drops DISPLAY and XDG_RUNTIME_DIR, which takes out X11 and
    # Wayland together and leaves SDL with nothing but an invisible driver.
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        reasons.append(
            "Running under sudo, which strips DISPLAY and XDG_RUNTIME_DIR from "
            "the environment -- that alone disables both X11 and Wayland. This "
            "machine does not need root: put your user in the 'gpio', 'video', "
            "'render' and 'input' groups and run it as yourself. If you really "
            "must use sudo, 'sudo -E' preserves the environment."
        )

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        reasons.append(
            "XDG_RUNTIME_DIR is not set, so the Wayland driver cannot start "
            "(this is the 'XDG_RUNTIME_DIR is invalid or not set' message). "
            "It is normally /run/user/$(id -u), created at login -- it goes "
            "missing under sudo, in a bare VT, or under a service with no "
            "session."
        )
    elif not os.path.isdir(runtime_dir):
        reasons.append(
            f"XDG_RUNTIME_DIR points at {runtime_dir!r}, which does not exist, "
            f"so the Wayland driver cannot start."
        )

    if not os.environ.get("DISPLAY"):
        reasons.append(
            "DISPLAY is unset, so SDL cannot use X11. If X is running on the "
            "television, run:  DISPLAY=:0 python3 main.py --real"
        )

    x_running = any(
        name.startswith("X") for name in _safe_listdir("/tmp/.X11-unix")
    )
    if x_running:
        reasons.append(
            "An X server is running. While X owns the display, SDL cannot take "
            "it over with kmsdrm -- so with no DISPLAY set there is nothing "
            "left for it to use. Either point at X with DISPLAY=:0, or stop X "
            "and boot to the console (see the README)."
        )

    if not os.path.exists("/dev/dri/card0"):
        reasons.append(
            "/dev/dri/card0 is missing, so the kmsdrm driver is unavailable "
            "too. On a Pi that usually means the KMS overlay is not enabled in "
            "config.txt."
        )
    elif not os.access("/dev/dri/card0", os.R_OK | os.W_OK):
        reasons.append(
            "/dev/dri/card0 exists but is not readable/writable by this user, "
            "so kmsdrm cannot open it. Add yourself to the 'video' and "
            "'render' groups:  sudo usermod -aG video,render $USER"
        )

    return reasons


def _safe_listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def warn_if_invisible() -> bool:
    """Shout if the game is about to render somewhere nobody can see.

    Not fatal. An explicitly requested invisible driver is legitimate -- it is
    how the headless test scripts run -- and even an accidental one should
    still let the machine come up so the log can be read. But it must never be
    silent: "runs fine, screen never changes" is otherwise indistinguishable
    from a dozen unrelated faults.
    """
    driver = pygame.display.get_driver()
    if driver not in INVISIBLE_DRIVERS:
        return False

    requested = os.environ.get("SDL_VIDEODRIVER")
    print(
        f"[ui] WARNING: video driver '{driver}' draws to nothing anybody can "
        f"see. The game will run perfectly and the screen will never change.",
        flush=True,
    )
    if requested in INVISIBLE_DRIVERS:
        print(
            f"[ui]   SDL_VIDEODRIVER={requested} asked for this. Unset it to "
            f"use a real display.",
            flush=True,
        )
        return True

    print("[ui]   SDL fell back to it because no real driver would open:", flush=True)
    for reason in diagnose_display() or ["no specific cause found."]:
        print(f"[ui]     - {reason}", flush=True)
    return True


def request_audio_format() -> None:
    """Ask for our mixer format BEFORE pygame.init(). Must be called first.

    This is the whole trick, and getting it wrong cost a black screen on a real
    cabinet. The tempting version is to let pygame.init() open the mixer however
    it likes, then quit() and re-init() it with the settings we want. On a PC
    that works. On a Pi driving audio out of the AV jack it can HANG: the device
    has not finished releasing when the reopen asks for it, ALSA blocks, and
    because a hang raises nothing there is no exception to catch. The game never
    reaches its main loop and the television just stays dark.

    pre_init() instead records the format we want and lets pygame.init() open
    the device exactly once. pygame.init() does not raise when the mixer fails --
    it reports failures in its return value -- so a cabinet with no working
    audio silently gets no mixer, and everything downstream already copes.
    """
    if not config.SOUND_ENABLED:
        return
    try:
        pygame.mixer.pre_init(
            frequency=audio.SAMPLE_RATE,
            size=-16,  # signed 16-bit, matching audio.to_pcm()
            channels=1,  # mono: it is a television speaker
            buffer=config.SOUND_BUFFER,
        )
    except Exception:  # pragma: no cover - pre_init is not supposed to fail
        pass


def init_audio() -> str:
    """Render audio.py's library into whatever mixer pygame.init() managed to
    open. Returns a status line for the boot log; never raises, never blocks.

    Sound is the one subsystem here allowed to simply not exist. No audio
    configured, a busy device, a cabinet with no speaker -- all of them end up
    here reporting a reason, and the game plays on in silence rather than
    refusing to start over a jingle.
    """
    if not config.SOUND_ENABLED:
        return "sound disabled in config"
    if not pygame.mixer.get_init():
        # pygame.init() could not open a device. Nothing to do and nothing to
        # fix: do NOT try to open one here, that is what used to hang.
        return "no audio device; running silent"
    try:
        pygame.mixer.set_num_channels(config.SOUND_CHANNELS)
        for name, pcm in audio.build_library(config.SOUND_VOLUME).items():
            _sounds[name] = pygame.mixer.Sound(buffer=pcm)
    except Exception as exc:  # a jingle may not take the machine down
        _sounds.clear()
        return f"could not build sounds ({exc}); running silent"
    return f"{len(_sounds)} sounds synthesised"


def play_sound(name: str) -> None:
    """Fire and forget. Unknown names and a dead mixer are both no-ops.

    Never allowed to raise: every call site is in the middle of a hand or a
    payout, and none of them should have to care whether the cabinet has a
    speaker attached.
    """
    sound = _sounds.get(name)
    if sound is None:
        return
    try:
        sound.play()
    except pygame.error:
        pass


# ---------------------------------------------------------------------------
# One card on the felt
# ---------------------------------------------------------------------------


@dataclass
class CardSprite:
    """A card being drawn, somewhere between the shoe and its place in a hand.

    `card is None` means the face is genuinely unknown to the renderer -- the
    dealer's hole card. It stays None until game.py stops hiding it, which is
    what triggers the turn-over flip.
    """

    card: Card | None
    origin: tuple[float, float]
    dest: tuple[float, float]
    move: Tween
    #: True while flying out of the shoe: adds the tilt and the size-up.
    is_deal: bool = False
    #: The back-to-face turn. None means "never been flipped" (hole card).
    flip: Tween | None = None
    #: Whether the landing click has been played for this card yet.
    landed: bool = False

    def pos(self, now_ms: int) -> tuple[float, float]:
        t = self.move.value(now_ms)
        return (
            anim.lerp(self.origin[0], self.dest[0], t),
            anim.lerp(self.origin[1], self.dest[1], t),
        )

    def slide_to(self, dest: tuple[float, float], now_ms: int, duration_ms: int) -> None:
        """Re-home a settled card when the hand re-centres around it."""
        self.origin = self.pos(now_ms)
        self.dest = dest
        self.move = Tween(now_ms, duration_ms, anim.ease_out_cubic)
        self.is_deal = False

    def reveal(self, card: Card, now_ms: int, duration_ms: int) -> None:
        self.card = card
        self.flip = Tween(now_ms, duration_ms, anim.linear)

    def busy(self, now_ms: int) -> bool:
        if not self.move.done(now_ms):
            return True
        return self.flip is not None and not self.flip.done(now_ms)


#: Arrow keys -> (dx, dy) for aligning the picture on a CRT. Built lazily on
#: first use of UI, since pygame constants need pygame imported (it is, above)
#: but key_code() needs no display.
_NUDGE_KEYS: dict[int, tuple[int, int]] = {
    pygame.K_LEFT: (-1, 0),
    pygame.K_RIGHT: (1, 0),
    pygame.K_UP: (0, -1),
    pygame.K_DOWN: (0, 1),
}


class UI:
    def __init__(self, fullscreen: bool = False) -> None:
        # Order matters: the mixer format has to be requested before init.
        request_audio_format()
        pygame.init()
        pygame.display.set_caption("Blackjack")
        pygame.mouse.set_visible(False)  # it's a cabinet, there's no mouse

        self.fullscreen = fullscreen
        self.offset_x = config.IMAGE_OFFSET_X
        self.offset_y = config.IMAGE_OFFSET_Y
        self.display: pygame.Surface  # set by _make_screen
        self.screen = self._make_screen(fullscreen)
        self.clock = pygame.time.Clock()
        self.show_safe_guide = config.SHOW_SAFE_AREA_GUIDE
        # Printed before anything optional runs, so that if the machine ever
        # does hang during startup again, the log says whether it got a screen.
        #
        # The DRIVER is the important half. "the game runs but the television
        # still shows the terminal" is not one fault but several, and they are
        # only distinguishable from here: `dummy` renders into the void,
        # `kmsdrm`/`fbcon` draw straight to a console that may not be the one
        # currently displayed, and `x11` needs a DISPLAY that is actually on
        # the tube. Guessing between them from across a room is hopeless.
        print(
            f"[ui] display ready: driver={pygame.display.get_driver()} "
            f"size={self.screen.get_size()} "
            f"{'fullscreen' if fullscreen else 'windowed'} "
            f"DISPLAY={os.environ.get('DISPLAY') or '<unset>'} "
            f"SDL_VIDEODRIVER={os.environ.get('SDL_VIDEODRIVER') or '<unset>'}",
            flush=True,
        )
        warn_if_invisible()

        print(f"[audio] {init_audio()}", flush=True)

        self.font_huge = pygame.font.Font(config.FONT_PATH, config.FONT_SIZE_HUGE)
        self.font_large = pygame.font.Font(config.FONT_PATH, config.FONT_SIZE_LARGE)
        self.font_medium = pygame.font.Font(config.FONT_PATH, config.FONT_SIZE_MEDIUM)
        self.font_small = pygame.font.Font(config.FONT_PATH, config.FONT_SIZE_SMALL)

        # config stores key NAMES (strings) so it can stay pygame-free; resolve
        # them to keycodes exactly once, here.
        self.key_to_button = {
            pygame.key.key_code(name): button
            for name, button in config.KEY_BINDINGS.items()
        }
        self.key_insert_coin = pygame.key.key_code(config.KEY_TEST_INSERT_COIN)
        self.key_coin_drop = pygame.key.key_code(config.KEY_TEST_COIN_DROP)
        self.key_jam_toggle = pygame.key.key_code(config.KEY_TEST_JAM_TOGGLE)
        self.key_fullscreen = pygame.key.key_code(config.KEY_TOGGLE_FULLSCREEN)
        self.key_safe_guide = pygame.key.key_code(config.KEY_TOGGLE_SAFE_GUIDE)
        self.key_screenshot = pygame.key.key_code(config.KEY_SCREENSHOT)
        self.key_quit = pygame.key.key_code(config.KEY_QUIT)

        # --- animation state ------------------------------------------------
        self.animate = config.ANIMATIONS_ENABLED
        self.now = 0
        self._first_update = True
        #: Milliseconds spent DRAWING the last frame, excluding the frame
        #: limiter's sleep. --profile reports it; nothing else reads it.
        self.last_draw_ms = 0.0
        self._draw_started = time.perf_counter()

        # Card faces are expensive to draw and there are only 53 of them, so
        # each one is rendered once into a surface and then blitted, scaled, or
        # rotated. Matters on a Pi: without this the flip would redraw pips
        # every frame.
        self._card_cache: dict[Card | None, pygame.Surface] = {}

        self.rows: dict[str, list[CardSprite]] = {"dealer": [], "player": []}
        self._discards: list[CardSprite] = []
        #: Earliest time the next card may leave the shoe -- this is the stagger.
        self._deal_clock_ms = 0
        #: Times at which cards left the shoe, so the stack can jolt.
        self._shoe_kicks: list[int] = []

        self._credits = RollingCounter(duration_ms=config.COUNTER_ROLL_MS)
        self._last_balance = 0
        self._credit_flash_ms = -10_000
        self._last_bet = config.MIN_BET_QUARTERS
        self._bet_bump_ms = -10_000
        self._last_shuffle_count = 0
        self._shuffle_notice_ms = -10_000
        self._last_owed = 0
        self._payout_coins: list[tuple[int, int]] = []  # (spawned_ms, x)
        self._banner_text = ""
        self._banner_ms = 0
        self._banner_lost = False
        self._jam_since_ms = -10_000

        #: Set by main.py when the test-coin key is armed on real hardware.
        self.test_coins_armed = False

        self._last_card_sound_ms = -10_000

        self._attract_active = False
        self._attract_started_ms = 0
        self._attract_pips = self._build_attract_pips()
        self._marquee = self.font_small.render(
            config.ATTRACT_MARQUEE_TEXT, True, config.COLOR_TEXT_DIM
        )

    def _make_screen(self, fullscreen: bool) -> pygame.Surface:
        """Open the display and decide where the game actually draws.

        With no image offset the game draws straight onto the display surface
        and nothing is copied. With an offset there is an intermediate canvas,
        blitted across by the offset each frame -- one full-surface blit, which
        is the price of being able to centre the picture on a tube that cannot
        centre itself.
        """
        flags = pygame.SCALED  # letterbox if the framebuffer isn't 640x480
        if fullscreen:
            flags |= pygame.FULLSCREEN
        size = (config.SCREEN_WIDTH, config.SCREEN_HEIGHT)
        self.display = pygame.display.set_mode(size, flags)
        if self.offset_x or self.offset_y:
            return pygame.Surface(size)
        return self.display

    def _rebuild_target(self) -> None:
        """Swap between drawing direct and drawing through a canvas, after the
        offset changes from nothing to something or back."""
        size = (config.SCREEN_WIDTH, config.SCREEN_HEIGHT)
        needs_canvas = bool(self.offset_x or self.offset_y)
        if needs_canvas and self.screen is self.display:
            self.screen = pygame.Surface(size)
        elif not needs_canvas and self.screen is not self.display:
            self.screen = self.display

    def nudge_image(self, dx: int, dy: int) -> None:
        """Shift the picture, and say where it ended up.

        Printing the numbers is the point: this is a service adjustment made
        once per television, and it is worthless if it cannot be written back
        into config.py afterwards.
        """
        self.offset_x += dx
        self.offset_y += dy
        self._rebuild_target()
        print(
            f"[ui] image offset now ({self.offset_x}, {self.offset_y}) -- put "
            f"this in config.py:  IMAGE_OFFSET_X = {self.offset_x}  "
            f"IMAGE_OFFSET_Y = {self.offset_y}",
            flush=True,
        )

    def _present(self) -> None:
        """Push the finished frame to the display, offset if need be."""
        if self.screen is not self.display:
            self.display.fill(config.COLOR_BG)
            self.display.blit(self.screen, (self.offset_x, self.offset_y))
        pygame.display.flip()

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        self.screen = self._make_screen(self.fullscreen)  # rebuilds .display too

    def quit(self) -> None:
        pygame.quit()

    # -- the animation switch ---------------------------------------------
    # Every duration and every cyclic effect goes through one of these, so
    # config.ANIMATIONS_ENABLED = False really does mean *nothing moves* --
    # no second code path, no branch that somebody forgets to add.

    def _dur(self, ms: int) -> int:
        """A duration, or 0 when animation is off (which snaps the tween)."""
        return ms if self.animate else 0

    def _fade(self, since_ms: int, duration_ms: int) -> float:
        """1.0 at `since_ms` decaying to 0.0 -- the flash-and-settle ramp."""
        duration = self._dur(duration_ms)
        if duration <= 0:
            return 0.0
        return anim.clamp01(1.0 - (self.now - since_ms) / duration)

    def _pulse(self, period_ms: int, phase: float = 0.0) -> float:
        """0..1 breathing. Parks at the midpoint when animation is off."""
        return anim.pulse(self.now, period_ms, phase) if self.animate else 0.5

    def _wave(self, period_ms: int, phase: float = 0.0) -> float:
        return anim.wave(self.now, period_ms, phase) if self.animate else 0.0

    def _blink(self, period_ms: int, duty: float = 0.5) -> bool:
        """Blinking text stays LIT when animation is off -- never hidden."""
        return anim.blink(self.now, period_ms, duty) if self.animate else True

    def _shake(self, since_ms: int, duration_ms: int, amplitude: float,
               cycles: float = 3.5) -> float:
        if not self.animate:
            return 0.0
        return anim.decaying_shake(self.now, since_ms, duration_ms, amplitude, cycles)

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def process_input(self, hardware) -> bool:
        """Pump pygame events. Returns False when the player wants to quit.

        Arcade buttons and their keyboard mirrors both funnel into
        hardware.inject_button(), so main.py sees one uniform event stream and
        never branches on which backend is live. The coin/drop test keys are
        inert on real hardware -- the base class makes them no-ops there.
        """
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type != pygame.KEYDOWN:
                continue

            key = event.key
            if key == self.key_quit:
                return False
            if key in self.key_to_button:
                hardware.inject_button(self.key_to_button[key])
            elif key == self.key_insert_coin:
                hardware.simulate_coin_insert()
            elif key == self.key_coin_drop:
                hardware.simulate_coin_drop()
            elif key == self.key_jam_toggle:
                hardware.toggle_simulated_jam()
            elif key == self.key_fullscreen:
                self.toggle_fullscreen()
            elif key == self.key_safe_guide:
                self.show_safe_guide = not self.show_safe_guide
            elif key == self.key_screenshot:
                self.save_screenshot()
            elif key in _NUDGE_KEYS:
                # Service adjustment. The cabinet's five arcade buttons cannot
                # reach these, so a keyboard being plugged in is the signal
                # that somebody is deliberately setting the machine up.
                dx, dy = _NUDGE_KEYS[key]
                step = config.IMAGE_NUDGE_STEP
                self.nudge_image(dx * step, dy * step)
        return True

    def save_screenshot(self) -> str | None:
        """Dump the current frame to a PNG next to the state file.

        Deliberately saves the 640x480 surface the game drew, NOT what the
        television made of it. That is the whole value: comparing this against
        what the tube shows tells you whether a problem is in the rendering or
        in the display chain -- overscan, a mode the TV cannot lock onto, a
        contrast setting -- which you cannot tell apart by squinting at a CRT.
        """
        # Second-resolution timestamps collide when the key is pressed twice
        # in a second, which is exactly what someone does when they are trying
        # to catch a glitch. Disambiguate rather than silently overwrite.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(config.STATE_DIR, f"screenshot-{stamp}.png")
        suffix = 2
        while os.path.exists(path):
            path = os.path.join(config.STATE_DIR, f"screenshot-{stamp}-{suffix}.png")
            suffix += 1
        try:
            os.makedirs(config.STATE_DIR, exist_ok=True)
            pygame.image.save(self.screen, path)
        except (pygame.error, OSError) as exc:
            print(f"[ui] could not save screenshot: {exc}", flush=True)
            return None
        print(f"[ui] screenshot saved: {path}", flush=True)
        return path

    # ------------------------------------------------------------------
    # Animation state -- called once per frame, BEFORE render
    # ------------------------------------------------------------------

    def update(self, game: BlackjackGame, bank, payout, now_ms: int) -> None:
        """Fold this frame's game state into the animation state.

        Deliberately a one-way read: the UI diffs what it sees against what it
        drew last frame and starts whatever motion that implies. Nothing is
        pushed in from main.py, so there is no way for a missed notification to
        leave a card unaccounted for.
        """
        self.now = now_ms

        if self._first_update:
            # Boot straight into the truth -- don't roll the meter up from zero
            # in front of a player whose money survived a power cut.
            self._credits.snap(bank.balance_quarters)
            self._last_balance = bank.balance_quarters
            self._last_bet = game.bet_quarters
            self._last_shuffle_count = game.shoe.shuffle_count
            self._last_owed = bank.owed_quarters
            self._first_update = False

        if bank.balance_quarters != self._last_balance:
            self._credits.set(bank.balance_quarters, now_ms)
            self._credit_flash_ms = now_ms
            self._last_balance = bank.balance_quarters

        if game.bet_quarters != self._last_bet:
            self._bet_bump_ms = now_ms
            self._last_bet = game.bet_quarters

        if game.shoe.shuffle_count != self._last_shuffle_count:
            self._shuffle_notice_ms = now_ms
            self._last_shuffle_count = game.shoe.shuffle_count
            play_sound("shuffle")

        # One coin fell out of the dispenser: drop one on screen to match.
        if bank.owed_quarters < self._last_owed:
            for _ in range(self._last_owed - bank.owed_quarters):
                self._payout_coins.append(
                    (now_ms, config.SCREEN_WIDTH - 74 + random.randint(-6, 6))
                )
            play_sound("dispense")
        self._last_owed = bank.owed_quarters

        if payout.status.jammed:
            if self._jam_since_ms < 0:
                self._jam_since_ms = now_ms
                play_sound("jam")
        else:
            self._jam_since_ms = -10_000

        self._sync_cards(game, now_ms)
        self._play_landing_clicks(now_ms)
        self._prune(now_ms)

    def revealed(self, name: str) -> tuple[list[Card], bool]:
        """(faces the player can actually see, whether any are still hidden).

        The hand totals on screen are computed from THIS, not from game.py's
        cards -- printing "YOU 18" over two face-down cards would give the deal
        away before it finishes. A face counts as seen from half way through
        its flip, which is the same instant the drawing code shows it.
        """
        shown: list[Card] = []
        hidden = False
        for sprite in self.rows[name]:
            if sprite.card is not None and sprite.flip is not None \
                    and sprite.flip.progress(self.now) >= 0.5:
                shown.append(sprite.card)
            else:
                hidden = True
        return shown, hidden

    def _play_landing_clicks(self, now_ms: int) -> None:
        """Click once per card, at the moment it touches the felt.

        Driven from the animation rather than from the button press in main.py,
        so a four-card deal is four separate clicks in the right rhythm instead
        of one noise while the cards are still in the air.
        """
        for row in self.rows.values():
            for sprite in row:
                if sprite.landed or not sprite.move.done(now_ms):
                    continue
                sprite.landed = True
                if now_ms - self._last_card_sound_ms >= config.SOUND_CARD_MIN_GAP_MS:
                    self._last_card_sound_ms = now_ms
                    play_sound("card")

    def is_dealing(self) -> bool:
        """True while a card is still on its way to the table or turning over.

        main.py waits on this before letting the dealer act, and before it
        starts the result-display timer -- the banner should not beat the cards
        that produced it onto the screen.
        """
        return any(
            sprite.busy(self.now)
            for row in self.rows.values()
            for sprite in row
        )

    def set_attract(self, active: bool) -> None:
        if active and not self._attract_active:
            self._attract_started_ms = self.now
        self._attract_active = active

    # -- card bookkeeping ------------------------------------------------

    def _dealer_row(self, game: BlackjackGame) -> list[Card | None]:
        row: list[Card | None] = list(game.dealer_visible_cards)
        if game.dealer_hole_hidden and len(game.dealer_cards) > 1:
            row.append(None)  # the hole card
        return row

    def _sync_cards(self, game: BlackjackGame, now_ms: int) -> None:
        rows: dict[str, list[Card | None]] = {
            "player": list(game.player_cards),
            "dealer": self._dealer_row(game),
        }

        if not rows["player"] and not rows["dealer"]:
            self._sweep_table(now_ms)
            self._deal_clock_ms = 0
            return

        if self._is_a_different_hand(rows):
            # An impatient player can press DEAL on the result screen, which
            # clears and re-deals inside a single frame -- the table never
            # looks empty from here. Clear it ourselves or the last hand's
            # cards would linger under the new one.
            self._sweep_table(now_ms)
            self._deal_clock_ms = 0

        # 1. Cards that appeared since the last frame. game.deal() hands us all
        #    four at once; sorting by (position, player-before-dealer) walks
        #    them out of the shoe the way a live dealer would.
        fresh: list[tuple[int, int, str, Card | None]] = []
        for name, cards in rows.items():
            for index in range(len(self.rows[name]), len(cards)):
                fresh.append((index, DEAL_PRIORITY[name], name, cards[index]))
        for _index, _priority, name, card in sorted(fresh, key=lambda f: (f[0], f[1])):
            self.rows[name].append(self._deal_sprite(card, now_ms))

        # 2. Cards that turned over -- in practice only ever the hole card.
        for name, cards in rows.items():
            for sprite, card in zip(self.rows[name], cards):
                if card is not None and sprite.card is None:
                    sprite.reveal(card, now_ms, self._dur(config.CARD_FLIP_MS))

        # 3. A growing hand re-centres, so everything already on the felt
        #    shuffles sideways to make room.
        for name, top in (("dealer", ROW_DEALER_CARDS), ("player", ROW_PLAYER_CARDS)):
            self._layout_row(self.rows[name], top, now_ms)

    def _is_a_different_hand(self, rows: dict[str, list[Card | None]]) -> bool:
        """True when what's on screen can no longer be the hand we're given.

        Growth is normal (a hit, the dealer drawing). A row getting SHORTER, a
        card changing under a sprite, or a face-up card going back down all
        mean a new hand was dealt without us ever seeing the table empty.
        """
        for name, cards in rows.items():
            sprites = self.rows[name]
            if len(sprites) > len(cards):
                return True
            for sprite, card in zip(sprites, cards):
                if sprite.card is None:
                    continue  # hole card, still waiting to be turned over
                if card is None or sprite.card != card:
                    return True
        return False

    def _deal_sprite(self, card: Card | None, now_ms: int) -> CardSprite:
        start = max(now_ms, self._deal_clock_ms)
        self._deal_clock_ms = start + self._dur(config.CARD_DEAL_STAGGER_MS)
        self._shoe_kicks.append(start)

        move = Tween(start, self._dur(config.CARD_FLY_MS), anim.ease_out_cubic)
        sprite = CardSprite(
            card=card,
            origin=self._shoe_origin(),
            dest=self._shoe_origin(),  # replaced by _layout_row before drawing
            move=move,
            is_deal=True,
        )
        if card is not None:
            # Face-down out of the shoe, turned over as it lands. The hole card
            # gets no flip -- it waits for game.py to stop hiding it.
            sprite.flip = Tween(
                move.end_ms, self._dur(config.CARD_FLIP_MS), anim.linear
            )
        return sprite

    def _shoe_origin(self) -> tuple[float, float]:
        """Top-left a card would have if it were centred on the shoe."""
        sx, sy = config.SHOE_POS
        cx = sx + config.CARD_WIDTH * config.SHOE_SCALE / 2
        cy = sy + config.CARD_HEIGHT * config.SHOE_SCALE / 2
        return (cx - config.CARD_WIDTH / 2, cy - config.CARD_HEIGHT / 2)

    def _row_positions(self, count: int, top: int) -> list[tuple[float, float]]:
        if count <= 0:
            return []
        spread = config.CARD_WIDTH + 8
        if count * spread > config.SAFE_RECT[2]:
            spread = config.CARD_SPACING  # fan them out overlapping instead
        total = spread * (count - 1) + config.CARD_WIDTH
        x = (config.SCREEN_WIDTH - total) // 2
        return [(x + i * spread, float(top)) for i in range(count)]

    def _layout_row(self, sprites: list[CardSprite], top: int, now_ms: int) -> None:
        for sprite, dest in zip(sprites, self._row_positions(len(sprites), top)):
            if sprite.dest == dest:
                continue
            if not sprite.move.started(now_ms):
                sprite.dest = dest  # still parked in the shoe: no visible jump
            elif sprite.move.done(now_ms):
                sprite.slide_to(dest, now_ms, self._dur(config.CARD_SLIDE_MS))
            else:
                sprite.dest = dest  # mid-flight: just bend the path

    def _sweep_table(self, now_ms: int) -> None:
        """Hand over: everything slides off to the discard tray."""
        for name in ("dealer", "player"):
            for i, sprite in enumerate(self.rows[name]):
                x, y = sprite.pos(now_ms)
                sprite.origin = (x, y)
                sprite.dest = (-config.CARD_WIDTH - 20.0, y - 26.0)
                sprite.move = Tween(
                    now_ms + self._dur(28 * i),
                    self._dur(config.CARD_SWEEP_MS),
                    anim.ease_in_cubic,
                )
                sprite.is_deal = False
                self._discards.append(sprite)
            self.rows[name] = []

    def _prune(self, now_ms: int) -> None:
        self._discards = [s for s in self._discards if not s.move.done(now_ms)]
        self._shoe_kicks = [k for k in self._shoe_kicks if now_ms - k < 400]
        self._payout_coins = [
            c
            for c in self._payout_coins
            if now_ms - c[0] < config.PAYOUT_COIN_FALL_MS
        ]

    # ------------------------------------------------------------------
    # Drawing helpers
    # ------------------------------------------------------------------

    def _text(
        self,
        text: str,
        font: pygame.font.Font,
        color: tuple[int, int, int],
        center_x: int | None = None,
        left: int | None = None,
        right: int | None = None,
        top: int = 0,
    ) -> pygame.Rect:
        surface = font.render(text, True, color)
        rect = surface.get_rect()
        rect.top = top
        if center_x is not None:
            rect.centerx = center_x
        elif right is not None:
            rect.right = right
        else:
            rect.left = left or 0
        self.screen.blit(surface, rect)
        return rect

    def _text_scaled(
        self,
        text: str,
        font: pygame.font.Font,
        color: tuple[int, int, int],
        center: tuple[float, float],
        scale: float = 1.0,
        angle: float = 0.0,
    ) -> pygame.Rect:
        """Text that can pop, wobble, or shake. Scaling costs a transform per
        frame, so only the handful of readouts that react use it."""
        surface = font.render(text, True, color)
        if scale != 1.0 or angle != 0.0:
            surface = pygame.transform.rotozoom(surface, angle, max(0.05, scale))
        rect = surface.get_rect(center=(int(center[0]), int(center[1])))
        self.screen.blit(surface, rect)
        return rect

    def _draw_suit(
        self,
        surface: pygame.Surface,
        suit: str,
        center: tuple[int, int],
        size: int,
        color: tuple[int, int, int],
    ) -> None:
        """Pips drawn as polygons/circles rather than glyphs.

        Unicode card suits aren't in every bundled font, and thin glyph strokes
        smear on composite anyway. Solid shapes always render and read cleanly.
        """
        cx, cy = center
        half = size // 2  # the diamond is the only suit still built from it

        if suit == "D":
            pygame.draw.polygon(
                surface,
                color,
                [(cx, cy - half), (cx + half, cy), (cx, cy + half), (cx - half, cy)],
            )
        elif suit == "H":
            self._draw_lobed_body(surface, cx, cy, size, color, point_down=True)
        elif suit == "S":
            # A spade is exactly a heart upside down, plus a stem.
            lobe_y, radius = self._draw_lobed_body(
                surface, cx, cy, size, color, point_down=False
            )
            self._draw_stem(
                surface, cx, lobe_y + round(radius * 0.3),
                cy + round(size * 0.5), size, color,
            )
        else:  # clubs
            # Three lobes in a triangle, overlapping just enough to join into
            # one shape. Spaced a hair further apart than they were: circles
            # this size with their centres only a radius apart merge into an
            # amorphous blob instead of reading as a clover.
            radius = max(2, round(size * 0.26))
            side_dx = max(1, round(size * 0.24))
            top_y = cy - round(size * 0.22)
            side_y = cy + round(size * 0.08)
            pygame.draw.circle(surface, color, (cx, top_y), radius)
            pygame.draw.circle(surface, color, (cx - side_dx, side_y), radius)
            pygame.draw.circle(surface, color, (cx + side_dx, side_y), radius)
            self._draw_stem(
                surface, cx, side_y, cy + round(size * 0.5), size, color
            )

    def _draw_lobed_body(
        self,
        surface: pygame.Surface,
        cx: int,
        cy: int,
        size: int,
        color: tuple[int, int, int],
        point_down: bool,
    ) -> tuple[int, int]:
        """Two round lobes tapering to a point: a heart, or a spade's body.

        ONE implementation, because a spade is a heart upside down and keeping
        two copies of that geometry is how they drifted apart in the first
        place. Both used to draw a triangle half a pixel wider than their own
        lobes, which left little spikes on the shoulders -- the tell that a
        card suit was built out of primitives rather than drawn.

        The fix is the `span` below: the triangle's base corners land exactly
        on the outer edge of the lobes, so the outline is continuous. Returns
        (lobe_y, radius) for the spade, which hangs its stem off them.
        """
        radius = max(2, round(size * 0.25))
        lobe_dx = max(1, round(radius * 0.9))
        span = lobe_dx + radius  # the silhouette's true half-width
        offset = round(size * 0.10)
        reach = round(size * 0.48)

        lobe_y = cy - offset if point_down else cy + offset
        tip_y = cy + reach if point_down else cy - reach

        pygame.draw.polygon(
            surface, color, [(cx - span, lobe_y), (cx + span, lobe_y), (cx, tip_y)]
        )
        pygame.draw.circle(surface, color, (cx - lobe_dx, lobe_y), radius)
        pygame.draw.circle(surface, color, (cx + lobe_dx, lobe_y), radius)
        return lobe_y, radius

    def _draw_stem(
        self,
        surface: pygame.Surface,
        cx: int,
        top: int,
        bottom: int,
        size: int,
        color: tuple[int, int, int],
    ) -> None:
        """The flared foot under a spade or a club.

        A plain rectangle -- which is what this used to be -- reads as a stick
        rather than a stem, and at pip size a 3px stick is exactly the kind of
        thin vertical the CRT shimmers on. The taper gives it some mass at the
        bottom where the eye expects it.
        """
        top_half = max(1, round(size * 0.07))
        bottom_half = max(2, round(size * 0.20))
        pygame.draw.polygon(
            surface,
            color,
            [
                (cx - top_half, top),
                (cx + top_half, top),
                (cx + bottom_half, bottom),
                (cx - bottom_half, bottom),
            ],
        )

    def _card_surface(self, card: Card | None) -> pygame.Surface:
        """One cached surface per card (None = the back). Built on first use."""
        cached = self._card_cache.get(card)
        if cached is not None:
            return cached

        surface = pygame.Surface(
            (config.CARD_WIDTH, config.CARD_HEIGHT), pygame.SRCALPHA
        )
        rect = surface.get_rect()

        if card is None:  # face down
            pygame.draw.rect(surface, config.COLOR_CARD_BACK, rect, border_radius=6)
            pygame.draw.rect(
                surface,
                config.COLOR_TEXT,
                rect,
                width=config.MIN_LINE_WIDTH,
                border_radius=6,
            )
            inner = rect.inflate(-18, -22)
            pygame.draw.rect(
                surface, config.COLOR_TEXT_DIM, inner, width=config.MIN_LINE_WIDTH
            )
        else:
            pygame.draw.rect(surface, config.COLOR_CARD_FACE, rect, border_radius=6)
            pygame.draw.rect(
                surface,
                config.COLOR_CARD_BLACK,
                rect,
                width=config.MIN_LINE_WIDTH,
                border_radius=6,
            )
            ink = config.COLOR_CARD_RED if card.is_red else config.COLOR_CARD_BLACK
            label = self.font_medium.render(card.rank, True, ink)
            surface.blit(label, (rect.left + 6, rect.top + 4))
            self._draw_suit(surface, card.suit, (rect.centerx, rect.centery + 14), 30, ink)

        self._card_cache[card] = surface
        return surface

    def _blit_card(
        self,
        card: Card | None,
        center: tuple[float, float],
        scale: float = 1.0,
        squash: float = 1.0,
        angle: float = 0.0,
    ) -> None:
        """Blit a cached card face, optionally mid-flip (squash) or tilted.

        `squash` is the horizontal scale: 1.0 flat on, 0.0 edge on.
        """
        surface = self._card_surface(card)
        w = max(2, int(config.CARD_WIDTH * scale * abs(squash)))
        h = max(2, int(config.CARD_HEIGHT * scale))
        if (w, h) != surface.get_size():
            surface = pygame.transform.smoothscale(surface, (w, h))
        if angle:
            surface = pygame.transform.rotate(surface, angle)
        self.screen.blit(
            surface, surface.get_rect(center=(int(center[0]), int(center[1])))
        )

    def _draw_sprite(self, sprite: CardSprite, now_ms: int) -> None:
        if not sprite.move.started(now_ms):
            return  # still in the shoe, waiting its turn

        x, y = sprite.pos(now_ms)
        center = (x + config.CARD_WIDTH / 2, y + config.CARD_HEIGHT / 2)

        scale, angle = 1.0, 0.0
        if sprite.is_deal and not sprite.move.done(now_ms):
            t = sprite.move.value(now_ms)
            scale = anim.lerp(config.CARD_DEAL_START_SCALE, 1.0, t)
            angle = anim.lerp(config.CARD_DEAL_ANGLE, 0.0, t)

        squash = 1.0
        show_face = sprite.card is not None
        if sprite.flip is not None and not sprite.flip.done(now_ms):
            t = sprite.flip.progress(now_ms)
            squash = abs(math.cos(math.pi * t))
            show_face = t >= 0.5
            scale *= 1.0 + 0.07 * math.sin(math.pi * t)  # a little lift off the felt

        self._blit_card(
            sprite.card if show_face else None, center, scale, squash, angle
        )

    def _draw_shoe(self, now_ms: int) -> None:
        """The stack the cards come out of. Jolts each time one leaves."""
        kick = sum(self._shake(k, 260, 3.5, cycles=2.0) for k in self._shoe_kicks)
        sx, sy = config.SHOE_POS
        w = config.CARD_WIDTH * config.SHOE_SCALE
        h = config.CARD_HEIGHT * config.SHOE_SCALE
        for i in range(3):
            self._blit_card(
                None,
                (sx + w / 2 + kick - i * 3, sy + h / 2 - i * 3),
                scale=config.SHOE_SCALE,
            )

    # ------------------------------------------------------------------
    # Frame
    # ------------------------------------------------------------------

    def render(self, game: BlackjackGame, bank, payout, notice: str | None = None) -> None:
        self._draw_started = time.perf_counter()
        if self._attract_active:
            self.render_attract()
            return

        now = self.now
        self.screen.fill(config.COLOR_BG)

        safe_x, safe_y, safe_w, safe_h = config.SAFE_RECT
        safe_right = safe_x + safe_w
        centre_x = config.SCREEN_WIDTH // 2

        # --- top bar: the two numbers that are always true --------------
        self._draw_credits(safe_x, safe_y, now)
        self._draw_bet(game, safe_right, safe_y, now)

        # A machine whose storage is stalling looks, to a player, exactly like
        # a machine that has crashed. Say which it is -- and say it to the
        # operator, because the file the card is struggling with is the one
        # holding the balance.
        if bank.slow_writes:
            self._text(
                f"STORAGE STALLING  {bank.worst_write_ms / 1000:.1f}s",
                self.font_small,
                config.COLOR_LOSE,
                center_x=centre_x,
                top=safe_y + 8,
            )
        elif self.test_coins_armed:
            # Between CREDITS and BET, where nothing else lives.
            self._text(
                "TEST COIN KEY ARMED",
                self.font_small,
                config.COLOR_LOSE,
                center_x=centre_x,
                top=safe_y + 8,
            )

        self._draw_shoe(now)

        # --- dealer -----------------------------------------------------
        # Totals count only what has actually been turned over on screen, so
        # they climb card by card as the hand is dealt.
        shown, hidden = self.revealed("dealer")
        dealer_label = "DEALER"
        if shown:
            dealer_label = f"DEALER  {hand_value(shown)[0]}{'+' if hidden else ''}"
        self._text(
            dealer_label,
            self.font_small,
            config.COLOR_TEXT_DIM,
            left=safe_x,
            top=ROW_DEALER_LABEL,
        )

        # --- player -----------------------------------------------------
        shown, hidden = self.revealed("player")
        player_label = "YOU"
        if shown:
            total, is_soft = hand_value(shown)
            soft = "/soft" if is_soft else ""
            player_label = f"YOU  {total}{soft}"
        self._text(
            player_label,
            self.font_small,
            config.COLOR_TEXT_DIM,
            left=safe_x,
            top=ROW_PLAYER_LABEL,
        )

        # --- the cards ---------------------------------------------------
        for sprite in self._discards:
            self._draw_sprite(sprite, now)
        for sprite in self.rows["dealer"]:
            self._draw_sprite(sprite, now)
        for sprite in self.rows["player"]:
            self._draw_sprite(sprite, now)

        # --- banner and prompt ------------------------------------------
        self._draw_banner(game, bank, payout, centre_x, now)

        prompt = notice or self._prompt_for(game, bank, payout)
        if prompt:
            self._draw_prompt(prompt, centre_x, now)

        # --- footer hints ------------------------------------------------
        self._draw_footer(game, bank, payout, centre_x, now)

        self._draw_payout_coins(now)

        # A jam is the one thing that must not be missable.
        if payout.status.jammed:
            self._draw_jam_overlay(bank, now)

        if self.show_safe_guide:
            pygame.draw.rect(
                self.screen, (255, 0, 255), pygame.Rect(*config.SAFE_RECT), width=2
            )

        self._present()
        self.last_draw_ms = (time.perf_counter() - self._draw_started) * 1000.0
        self.clock.tick(config.FPS)

    # -- top bar ----------------------------------------------------------

    def _draw_credits(self, safe_x: int, safe_y: int, now: int) -> None:
        """CREDITS rolls to its new value and flashes white when it changes."""
        flash = self._fade(self._credit_flash_ms, config.FLASH_MS)
        color = anim.mix_color(config.COLOR_ACCENT, config.COLOR_TEXT, flash)
        text = f"CREDITS {self._credits.display(now)}"
        surface = self.font_large.render(text, True, color)
        if flash > 0.0:
            scale = 1.0 + 0.10 * anim.ease_out_cubic(flash)
            surface = pygame.transform.rotozoom(surface, 0.0, scale)
        rect = surface.get_rect()
        rect.midleft = (safe_x, safe_y + self.font_large.get_height() // 2)
        self.screen.blit(surface, rect)

    def _draw_bet(self, game, safe_right: int, safe_y: int, now: int) -> None:
        bump = self._fade(self._bet_bump_ms, config.BET_BUMP_MS)
        scale = 1.0 + 0.22 * anim.ease_out_back(bump) * bump
        surface = self.font_large.render(
            f"BET {game.bet_quarters}", True, config.COLOR_ACCENT
        )
        if bump > 0.0:
            surface = pygame.transform.rotozoom(surface, 0.0, scale)
        rect = surface.get_rect()
        rect.midright = (safe_right, safe_y + self.font_large.get_height() // 2)
        self.screen.blit(surface, rect)

    # -- banner / prompt / footer -----------------------------------------

    def _draw_banner(self, game, bank, payout, centre_x: int, now: int) -> None:
        text, color = self._banner_for(game, bank, payout)
        if text != self._banner_text:
            self._banner_text = text
            self._banner_ms = now
            self._banner_lost = bool(
                game.result is not None
                and game.phase is Phase.SETTLED
                and not game.result.outcome.player_won
                and not game.result.outcome.is_push
            )
        if not text:
            return

        centre_y = ROW_BANNER + self.font_huge.get_height() // 2
        pop = Tween(self._banner_ms, self._dur(config.BANNER_POP_MS), anim.ease_out_back)
        scale = pop.at(now, 0.45, 1.0)
        dx = 0.0

        if self._banner_lost:
            # A loss knocks rather than springs: shake it and skip the bounce.
            scale = anim.lerp(
                0.75, 1.0, anim.ease_out_cubic(pop.progress(now))
            )
            dx = self._shake(
                self._banner_ms, config.BANNER_SHAKE_MS, config.BANNER_SHAKE_PX
            )
        elif pop.done(now) and color == config.COLOR_WIN:
            scale = 1.0 + 0.035 * self._pulse(900)

        self._text_scaled(
            text, self.font_huge, color, (centre_x + dx, centre_y), scale=scale
        )

    def _draw_prompt(self, prompt: str, centre_x: int, now: int) -> None:
        color = config.COLOR_TEXT
        if prompt == "INSERT QUARTERS":
            # The classic. A hard blink reads from across the room; a fade does not.
            if not self._blink(config.PROMPT_BLINK_MS, 0.6):
                return
            color = config.COLOR_ACCENT
        elif prompt == "PRESS DEAL":
            color = anim.mix_color(
                config.COLOR_TEXT_DIM, config.COLOR_TEXT, self._pulse(1500)
            )
        elif prompt == "DEALER DRAWS" and self.animate:
            prompt = "DEALER DRAWS" + "." * (1 + (now // 320) % 3)

        self._text(
            prompt, self.font_medium, color, center_x=centre_x, top=ROW_PROMPT
        )

    def _draw_footer(self, game, bank, payout, centre_x: int, now: int) -> None:
        """The hint line, or the shuffle notice riding over it."""
        age = now - self._shuffle_notice_ms
        if 0 <= age < config.SHUFFLE_NOTICE_MS:
            slide = Tween(self._shuffle_notice_ms, self._dur(240)).value(now)
            fade = anim.clamp01((config.SHUFFLE_NOTICE_MS - age) / 300)
            color = anim.mix_color(config.COLOR_BG, config.COLOR_ACCENT, fade)
            x = anim.lerp(centre_x - 120, centre_x, slide)
            self._text_scaled(
                "SHUFFLING THE SHOE",
                self.font_small,
                color,
                (x, ROW_HINTS + self.font_small.get_height() // 2),
            )
            return

        hints = self._hints_for(game, bank, payout)
        if hints:
            self._text(
                hints,
                self.font_small,
                config.COLOR_TEXT_DIM,
                center_x=centre_x,
                top=ROW_HINTS,
            )

    def _draw_payout_coins(self, now: int) -> None:
        """One tumbling quarter per coin the dispenser has actually confirmed.

        Driven by bank.owed_quarters going down, so what you see on screen is
        what the hardware says it paid -- never an optimistic guess.
        """
        if not self.animate:
            return
        for spawned, x in self._payout_coins:
            t = anim.clamp01((now - spawned) / config.PAYOUT_COIN_FALL_MS)
            y = anim.lerp(300.0, 462.0, anim.ease_in_quad(t))
            r = config.PAYOUT_COIN_RADIUS
            spin = max(2, int(r * abs(math.cos(t * 7.0))))
            rect = pygame.Rect(0, 0, spin * 2, r * 2)
            rect.center = (int(x), int(y))
            pygame.draw.ellipse(self.screen, config.COLOR_ACCENT, rect)
            if spin > 4:
                pygame.draw.ellipse(
                    self.screen, config.COLOR_BG_ALT, rect, width=config.MIN_LINE_WIDTH
                )

    def _banner_for(self, game, bank, payout):
        if payout.status.jammed:
            return "", config.COLOR_TEXT
        if self.is_dealing():
            # A natural settles the hand the instant it is dealt. Don't shout
            # BLACKJACK! over cards that are still in the air.
            return "", config.COLOR_TEXT
        if payout.is_active or bank.owed_quarters > 0:
            return f"PAYING {bank.owed_quarters}", config.COLOR_ACCENT
        if game.phase is Phase.SETTLED and game.result is not None:
            result = game.result
            if result.outcome.player_won:
                return result.message, config.COLOR_WIN
            if result.outcome.is_push:
                return result.message, config.COLOR_TEXT
            return result.message, config.COLOR_LOSE
        return "", config.COLOR_TEXT

    def _prompt_for(self, game, bank, payout):
        if payout.status.jammed or payout.is_active or bank.owed_quarters > 0:
            return ""
        if self.is_dealing():
            return ""  # asking HIT OR STAND? mid-deal reads as a stutter
        if game.phase is Phase.SETTLED and game.result is not None:
            net = game.result.net_quarters
            if net > 0:
                return f"+{net} QUARTERS"
            if net < 0:
                return f"{net} QUARTERS"
            return "BET RETURNED"
        if game.phase is Phase.PLAYER_TURN:
            return "HIT OR STAND?"
        if game.phase is Phase.DEALER_TURN:
            return "DEALER DRAWS"
        if bank.balance_quarters <= 0:
            return "INSERT QUARTERS"
        if bank.balance_quarters < game.bet_quarters:
            return "NOT ENOUGH CREDITS"
        return "PRESS DEAL"

    def _hints_for(self, game, bank, payout):
        if payout.status.jammed:
            return "CASH OUT = RETRY"
        if payout.is_active:
            return "DISPENSING..."
        if game.phase is Phase.PLAYER_TURN:
            return "HIT   STAND"
        if game.phase is Phase.BETTING:
            if bank.balance_quarters > 0:
                return "BET   DEAL   CASH OUT"
            return "BET 1-4 QUARTERS   BLACKJACK PAYS 3:2"
        return ""

    def _draw_jam_overlay(self, bank, now: int) -> None:
        # Shake on arrival, then flash the border for as long as it lasts.
        dx = self._shake(self._jam_since_ms, 420, 10.0)
        box = pygame.Rect(config.SAFE_RECT[0] + int(dx), 150, config.SAFE_RECT[2], 180)
        pygame.draw.rect(self.screen, config.COLOR_ALERT_BG, box)
        border = (
            config.COLOR_ACCENT
            if self._blink(config.JAM_FLASH_PERIOD_MS)
            else config.COLOR_TEXT
        )
        pygame.draw.rect(self.screen, border, box, width=config.MIN_LINE_WIDTH)
        centre_x = box.centerx
        self._text("DISPENSER JAM", self.font_large, config.COLOR_TEXT, center_x=centre_x, top=box.top + 16)
        self._text(
            f"COINS OWED: {bank.owed_quarters}",
            self.font_large,
            config.COLOR_ACCENT,
            center_x=centre_x,
            top=box.top + 66,
        )
        self._text(
            "CALL ATTENDANT  /  PRESS CASH OUT TO RETRY",
            self.font_small,
            config.COLOR_TEXT,
            center_x=centre_x,
            top=box.top + 124,
        )

    # ------------------------------------------------------------------
    # Attract screen -- the title/start menu
    # ------------------------------------------------------------------

    def _build_attract_pips(self) -> list[tuple[str, int, int, float, float]]:
        """(suit, x, size, phase, drift) for the pips drifting up the felt.

        Seeded, so the attract screen looks the same on every boot and any
        oddity is reproducible.
        """
        rng = random.Random(4242)
        suits = ("S", "H", "D", "C")
        return [
            (
                suits[i % 4],
                rng.randint(20, config.SCREEN_WIDTH - 20),
                rng.randint(16, 34),
                rng.random(),
                rng.uniform(6.0, 18.0),
            )
            for i in range(config.ATTRACT_PIP_COUNT)
        ]

    def render_attract(self) -> None:
        """Title screen: what the cabinet shows when nobody is playing.

        main.py only enters this with an empty credit meter and nothing owed,
        so it can never hide a player's money behind a pretty screen.
        """
        now = self.now
        self._draw_started = time.perf_counter()
        self.screen.fill(config.COLOR_BG)

        self._draw_attract_pips(now)
        self._draw_attract_title(now)
        self._draw_attract_cards(now)

        centre_x = config.SCREEN_WIDTH // 2
        if self._blink(config.PROMPT_BLINK_MS, 0.62):
            self._text(
                "INSERT QUARTER",
                self.font_large,
                config.COLOR_ACCENT,
                center_x=centre_x,
                top=ATTRACT_INSERT_TOP,
            )
        self._text_scaled(
            "PRESS ANY BUTTON TO START",
            self.font_medium,
            anim.mix_color(config.COLOR_TEXT_DIM, config.COLOR_TEXT, self._pulse(1800)),
            (centre_x, ATTRACT_START_TOP + self.font_medium.get_height() / 2),
        )

        self._draw_attract_marquee(now)

        if self.show_safe_guide:
            pygame.draw.rect(
                self.screen, (255, 0, 255), pygame.Rect(*config.SAFE_RECT), width=2
            )

        self._present()
        self.last_draw_ms = (time.perf_counter() - self._draw_started) * 1000.0
        self.clock.tick(config.FPS)

    def _draw_attract_pips(self, now: int) -> None:
        """Suits drifting slowly up the felt. Kept close to the background
        colour on purpose -- high-contrast moving detail crawls on composite."""
        color = anim.mix_color(config.COLOR_BG, config.COLOR_TEXT_DIM, 0.22)
        period = config.ATTRACT_PIP_PERIOD_MS
        for suit, x, size, phase, drift in self._attract_pips:
            t = ((now / period) + phase) % 1.0 if self.animate else phase
            y = anim.lerp(config.SCREEN_HEIGHT + size, -size, t)
            wobble = drift * self._wave(4200, phase)
            self._draw_suit(self.screen, suit, (int(x + wobble), int(y)), size, color)

    def _draw_attract_title(self, now: int) -> None:
        """B-L-A-C-K-J-A-C-K drops in one letter at a time, then ripples."""
        title = "BLACKJACK"
        widths = [self.font_huge.size(ch)[0] for ch in title]
        total = sum(widths)
        x = (config.SCREEN_WIDTH - total) / 2
        landed_ms = (
            self._attract_started_ms
            + config.ATTRACT_LETTER_STAGGER_MS * (len(title) - 1)
            + config.ATTRACT_LETTER_DROP_MS
        )

        for i, ch in enumerate(title):
            drop = Tween(
                self._attract_started_ms + self._dur(config.ATTRACT_LETTER_STAGGER_MS * i),
                self._dur(config.ATTRACT_LETTER_DROP_MS),
                anim.ease_out_back,
            )
            y = drop.at(now, ATTRACT_TITLE_TOP - 150.0, float(ATTRACT_TITLE_TOP))
            if now >= landed_ms:
                # Settled: a slow wave travelling along the word.
                y += config.ATTRACT_WAVE_PX * self._wave(
                    config.ATTRACT_WAVE_PERIOD_MS, -i * 0.09
                )
            # "BLACK" in white, "JACK" in gold -- reads as one word from a
            # distance and gives the eye somewhere to land up close.
            color = config.COLOR_TEXT if i < 5 else config.COLOR_ACCENT
            self.screen.blit(self.font_huge.render(ch, True, color), (int(x), int(y)))
            x += widths[i]

    def _draw_attract_cards(self, now: int) -> None:
        """An ace and a king slide in from the wings: a natural, of course."""
        specs = (
            # (card, from_x, to_x, resting angle, slide delay)
            (Card("A", "S"), -110.0, config.SCREEN_WIDTH / 2 - 46, 11.0, 220),
            (Card("K", "H"), config.SCREEN_WIDTH + 110.0, config.SCREEN_WIDTH / 2 + 46, -9.0, 380),
        )
        for i, (card, from_x, to_x, angle, delay) in enumerate(specs):
            slide = Tween(
                self._attract_started_ms + self._dur(delay),
                self._dur(config.ATTRACT_CARD_SLIDE_MS),
                anim.ease_out_cubic,
            )
            t = slide.value(now)
            x = anim.lerp(from_x, to_x, t)
            # Spin down to the resting tilt, then rock gently on the felt.
            rock = 2.2 * self._wave(3400, i * 0.3) if slide.done(now) else 0.0
            spin = anim.lerp(angle + 160.0 * (1 if i == 0 else -1), angle, t)
            y = ATTRACT_CARDS_Y + 6.0 * self._wave(3400, i * 0.3 + 0.25)
            self._blit_card(card, (x, y), scale=1.0, angle=spin + rock)

    def _draw_attract_marquee(self, now: int) -> None:
        """Endless ticker of the house rules along the bottom."""
        width = self._marquee.get_width()
        safe_x, _, safe_w, _ = config.SAFE_RECT
        offset = (
            int(now * config.ATTRACT_MARQUEE_PX_PER_S / 1000) % max(1, width)
            if self.animate
            else 0
        )
        band = pygame.Rect(safe_x, ATTRACT_MARQUEE_TOP - 4, safe_w, self._marquee.get_height() + 8)
        self.screen.set_clip(band)
        x = safe_x - offset
        while x < safe_x + safe_w:
            self.screen.blit(self._marquee, (x, ATTRACT_MARQUEE_TOP))
            x += width
        self.screen.set_clip(None)
