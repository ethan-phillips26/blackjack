"""
Central configuration for the coin-op blackjack machine.

DESIGN RULE: this module must stay importable with NO third-party packages.
No `import pygame`, no `import gpiozero`. game.py / bank.py / cards.py import
this file, and they must run on a bare Python install (see acceptance criteria).

That is why key bindings below are *strings*, not pygame key constants; ui.py
resolves them with pygame.key.key_code() at startup.
"""

import os

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Where the money state lives. Overridable so tests can point at a tmpdir.
STATE_DIR = os.environ.get("BLACKJACK_STATE_DIR", os.path.join(BASE_DIR, "state"))

# Single JSON file: {"balance_quarters": N, "owed_quarters": N, ...}
# Written atomically (temp file + fsync + os.replace). See bank.py.
BANK_STATE_PATH = os.path.join(STATE_DIR, "bank.json")

# Append-only audit trail of every coin in / coin out. Not authoritative --
# bank.json is -- but invaluable when a machine "eats" someone's money.
LEDGER_PATH = os.path.join(STATE_DIR, "ledger.log")

# --------------------------------------------------------------------------
# Display / CRT
# --------------------------------------------------------------------------

SCREEN_WIDTH = 640
SCREEN_HEIGHT = 480
FPS = 30  # composite CRT is 60i; 30fps is plenty and keeps the Pi cool.

# Fullscreen on the Pi, windowed on the PC (overridable by --fullscreen).
FULLSCREEN_DEFAULT_ON_PI = True
FULLSCREEN_DEFAULT_ON_PC = False

# Overscan-safe area. A CRT hides roughly the outer 5% of the raster, and how
# much varies per set -- tune these two numbers against YOUR TV.
# 0.05 * 640 = 32px left/right, 0.05 * 480 = 24px top/bottom.
SAFE_AREA_INSET_X_FRAC = 0.05
SAFE_AREA_INSET_Y_FRAC = 0.05

SAFE_X = int(SCREEN_WIDTH * SAFE_AREA_INSET_X_FRAC)
SAFE_Y = int(SCREEN_HEIGHT * SAFE_AREA_INSET_Y_FRAC)
SAFE_RECT = (SAFE_X, SAFE_Y, SCREEN_WIDTH - 2 * SAFE_X, SCREEN_HEIGHT - 2 * SAFE_Y)

# Draw a magenta box on the safe-area boundary to help you dial in the insets.
# Toggled at runtime with the debug key; this is just the startup default.
SHOW_SAFE_AREA_GUIDE = False

# --- Image position -------------------------------------------------------
# Nudges the whole picture, in 640x480 pixels. Positive X moves it right,
# positive Y moves it down.
#
# CRTs are not centred the way a monitor is: the deflection circuitry drifts
# with age and temperature, and most consumer sets have no horizontal position
# control outside a service menu. This is the same per-television adjustment as
# the safe-area inset above -- and unlike the overscan_* settings in the Pi's
# config.txt, it takes effect without a reboot.
#
# Tune it live with the arrow keys and copy the numbers it prints back here.
# Zero on both axes costs nothing at all: the game draws straight to the
# display and no copy happens.
IMAGE_OFFSET_X = 0
IMAGE_OFFSET_Y = 0

# --- Filling the screen ----------------------------------------------------
# pygame.SCALED preserves SQUARE-pixel aspect, so a 640x480 game on a 720x480
# composite mode gets black pillarbox bars down both sides. Those bars are
# drawn by SDL outside our surface, so IMAGE_OFFSET_* cannot move them and the
# felt colour never reaches them.
#
# That is also simply wrong for composite. NTSC 720x480 is a 4:3 picture made
# of NON-square pixels: stretching 640x480 across the full 720 is what makes it
# come out with the right geometry on the tube. Preserving square aspect both
# wastes the sides and squashes the image.
#
# True  = own the whole display and scale our 640x480 onto it. No black bars.
# False = the old pygame.SCALED behaviour, letterboxed, square pixels.
# Only applies fullscreen; a 640x480 window on a PC needs no scaling either way.
STRETCH_TO_FILL = True

# How far one arrow-key press moves it.
IMAGE_NUDGE_STEP = 2

# --- Composite-safe palette -----------------------------------------------
# Rules of thumb for composite video:
#   * Never pure #FFFFFF or #000000 -- clipping blooms and smears.
#   * Avoid saturated red/blue on a dark background (chroma crawl).
#   * Keep adjacent colors far apart in LUMA, not just hue.
COLOR_BG = (16, 48, 24)  # felt green, dark but not crushed black
COLOR_BG_ALT = (12, 36, 18)  # panel background
COLOR_TEXT = (232, 232, 224)  # off-white, not blown out
COLOR_TEXT_DIM = (150, 158, 150)
COLOR_ACCENT = (240, 200, 60)  # gold: bet / balance
COLOR_WIN = (110, 220, 120)
COLOR_LOSE = (224, 96, 88)  # desaturated red -- pure red smears on composite
COLOR_CARD_FACE = (228, 228, 220)
COLOR_CARD_BACK = (40, 72, 160)
COLOR_CARD_RED = (176, 32, 32)  # rank/suit ink on a light card = safe
COLOR_CARD_BLACK = (24, 24, 24)
COLOR_ALERT_BG = (128, 24, 24)  # jam / error banner

# Minimum stroke width. 1px lines shimmer badly on interlaced composite.
MIN_LINE_WIDTH = 3

# Fonts. None = pygame default (bundled, always present). Point a path here if
# you want a chunkier arcade face. Sizes are deliberately large for a 480-line
# display viewed from across a room.
FONT_PATH = None
FONT_SIZE_HUGE = 56  # win/lose banner
FONT_SIZE_LARGE = 36  # balance, bet, hand totals
FONT_SIZE_MEDIUM = 26  # prompts
FONT_SIZE_SMALL = 20  # footer / hints
FONT_BOLD = True

# Card geometry (px). Big enough to read the rank at 480 lines.
CARD_WIDTH = 72
CARD_HEIGHT = 100
CARD_SPACING = 24  # overlap step when a hand gets long

# --------------------------------------------------------------------------
# Game rules
# --------------------------------------------------------------------------

NUM_DECKS = 6
# Reshuffle when fewer than this fraction of the shoe remains (cut card).
SHOE_RESHUFFLE_THRESHOLD = 0.25

MIN_BET_QUARTERS = 1
MAX_BET_QUARTERS = 4

DEALER_HITS_SOFT_17 = False  # False = dealer stands on ALL 17s (spec default)

BLACKJACK_PAYOUT_NUMERATOR = 3  # 3:2
BLACKJACK_PAYOUT_DENOMINATOR = 2

# --- Quarter-rounding policy ----------------------------------------------
# Payouts are physical quarters, so a 3:2 natural on an odd bet (1 or 3) lands
# on a half quarter: bet 1 -> bonus 1.5 -> 1 or 2. There is no way to pay 1.5
# quarters, so the fraction must go somewhere. This constant decides where.
#   "down"    -- house keeps the half. Standard for coin machines. DEFAULT.
#   "up"      -- player gets the half. Player-favorable.
#   "nearest" -- banker's rounding, ties to even.
# The single implementation lives in game.blackjack_bonus_quarters(); change
# this constant, not the arithmetic.
BLACKJACK_ROUNDING = "down"

# EXTENSION POINT: double / split / insurance / surrender flags land here.
# Keep them off; the rules engine has TODO markers where they hook in.

# --------------------------------------------------------------------------
# Coin acceptor (CH-926 or similar pulse-output unit)
# --------------------------------------------------------------------------

# How many pulses the acceptor emits per accepted quarter. Programmable on the
# CH-926; 1 is the sane setting.
COIN_PULSES_PER_COIN = 1

# Ignore edges closer together than this -- mechanical/optical switch bounce.
COIN_PULSE_DEBOUNCE_MS = 20

# Only used when COIN_PULSES_PER_COIN > 1: pulses arriving within this window
# of each other belong to the same coin. Must be > the acceptor's inter-pulse
# gap (typically 40-100ms) and < the fastest a human can feed two coins.
COIN_PULSE_GROUP_WINDOW_MS = 250

# Most acceptors use an open-collector output that pulls LOW on a pulse.
COIN_PULSE_ACTIVE_LOW = True

QUARTER_VALUE_CENTS = 25  # display only -- accounting is in whole quarters.

# --- Test-coin key on real hardware ---------------------------------------
# Normally the PC-only test keys are inert on the Pi, because a keystroke that
# mints credits is a free-money bug rather than a debug aid. This flag re-arms
# ONE of them -- KEY_TEST_INSERT_COIN ('Q') -- on real hardware, so a keyboard
# can add credits without feeding the acceptor. Useful for bench-testing a
# built cabinet, and for an operator adding credits without opening the box.
#
# It is deliberately NOT a blanket "enable the test keys" switch:
#   * 'D' (fake IR coin drop) stays inert whatever this is set to. Confirming a
#     coin that never physically fell makes the machine believe it paid a
#     player it did not -- that loses somebody ELSE'S money, not the operator's.
#   * 'J' (jam simulation) stays inert too; on a real dispenser you can create
#     a jam by holding the slide.
#
# A test coin is still a real credit and can still be cashed out as a real
# quarter, so it is kept OUT of the cash-box accounting: it lands in the ledger
# as TEST_COIN_IN and counts toward lifetime_test_coins_in, never
# lifetime_coins_in. Cash box reconciliation stays honest either way.
#
# SET THIS BACK TO False BEFORE A CABINET GOES OUT IN PUBLIC. While it is True
# the machine says so on screen and in the boot log.
ALLOW_TEST_COINS_ON_REAL_HARDWARE = True

# --------------------------------------------------------------------------
# Coin-slide dispenser (solenoid)
# --------------------------------------------------------------------------

SOLENOID_ON_MS = 120  # coil energized -- push the slide
SOLENOID_RESET_MS = 220  # coil off -- let the spring return the slide
# => ~2.9 coins/sec. Lengthen both if the slide misses at speed.

# Set False if your driver board is active-low (many relay modules are).
SOLENOID_ACTIVE_HIGH = True

# Safety: never leave the coil energized longer than this, whatever happens.
# Continuous duty will cook a coin-slide solenoid.
SOLENOID_MAX_ON_MS = 500

# --- IR break-beam confirmation -------------------------------------------
# True  = closed loop: every coin must break the beam or it counts as a jam.
# False = open loop: assume one actuation == one coin. No sensor fitted.
USE_DROP_SENSOR = True

# Window after de-energizing in which we expect to see the coin fall past.
COIN_DROP_TIMEOUT_MS = 700
MAX_JAM_RETRIES = 3  # extra attempts for the SAME quarter before giving up

IR_SENSOR_ACTIVE_LOW = True  # beam broken pulls the line LOW
IR_SENSOR_DEBOUNCE_MS = 10

# --------------------------------------------------------------------------
# GPIO pin map (BCM numbering)
# --------------------------------------------------------------------------

PIN_COIN_ACCEPTOR = 17  # in,  pulled up, from acceptor via level shift
PIN_IR_SENSOR = 27  # in,  pulled up
PIN_SOLENOID = 18  # out, to MOSFET gate / relay IN -- NEVER the coil
PIN_BTN_BET = 5  # in,  pulled up, button to GND
PIN_BTN_DEAL = 6
PIN_BTN_HIT = 13
PIN_BTN_STAND = 19
PIN_BTN_CASHOUT = 26

BUTTON_DEBOUNCE_MS = 30
BUTTON_PULL_UP = True  # wire buttons between the pin and GND

# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------

# Logical button names. hardware/base.py emits these; ui.py and main.py match
# on them. Physical GPIO and PC keyboard both funnel into these five strings.
BTN_BET = "BET"
BTN_DEAL = "DEAL"
BTN_HIT = "HIT"
BTN_STAND = "STAND"
BTN_CASHOUT = "CASHOUT"

ALL_BUTTONS = (BTN_BET, BTN_DEAL, BTN_HIT, BTN_STAND, BTN_CASHOUT)

BUTTON_PINS = {
    BTN_BET: PIN_BTN_BET,
    BTN_DEAL: PIN_BTN_DEAL,
    BTN_HIT: PIN_BTN_HIT,
    BTN_STAND: PIN_BTN_STAND,
    BTN_CASHOUT: PIN_BTN_CASHOUT,
}

# Keyboard mirror of the arcade buttons. Values are pygame key NAMES, resolved
# via pygame.key.key_code() -- keeps this module pygame-free.
KEY_BINDINGS = {
    "b": BTN_BET,
    "return": BTN_DEAL,
    "h": BTN_HIT,
    "s": BTN_STAND,
    "c": BTN_CASHOUT,
}

# PC-only test keys. Ignored by the real backend; these are how you exercise
# the acceptor and dispenser logic with nothing wired up.
KEY_TEST_INSERT_COIN = "q"  # simulate one quarter accepted
KEY_TEST_COIN_DROP = "d"  # simulate the IR beam seeing a coin fall
KEY_TEST_JAM_TOGGLE = "j"  # stop auto-confirming drops -> force a jam
KEY_TOGGLE_FULLSCREEN = "f11"
KEY_TOGGLE_SAFE_GUIDE = "g"
# Saves exactly what the game is drawing to a PNG. The point of it is a CRT:
# when the tube shows something odd, this separates "the game is drawing the
# wrong thing" from "the television is not showing what the game drew", which
# are not otherwise distinguishable from across a room.
KEY_SCREENSHOT = "f12"
KEY_QUIT = "escape"

# Mock dispenser: auto-confirm each drop after this delay, so cash-out "just
# works" without hammering the drop key. Press KEY_TEST_JAM_TOGGLE to disable
# and exercise the jam path.
MOCK_AUTO_CONFIRM_DROPS = True
MOCK_AUTO_CONFIRM_DELAY_MS = 80

# --------------------------------------------------------------------------
# Animation
# --------------------------------------------------------------------------
#
# All animation is PURELY COSMETIC. It never gates a coin, a bet, or a payout:
# the rules engine and the bank run at full speed regardless, and setting
# ANIMATIONS_ENABLED = False must leave a fully playable machine. Timings are
# in milliseconds and are deliberately short -- an arcade cabinet has to feel
# snappy, and a 30fps composite CRT smears anything slow and floaty.

ANIMATIONS_ENABLED = True

# --- Dealing ---------------------------------------------------------------
# Cards fly out of the shoe (drawn top-right) to their place on the felt.
CARD_FLY_MS = 200  # shoe -> table for one card
CARD_DEAL_STAGGER_MS = 125  # gap between consecutive cards leaving the shoe
CARD_FLIP_MS = 150  # face-down -> face-up turn
CARD_SLIDE_MS = 170  # existing cards shuffling over as a hand grows
CARD_SWEEP_MS = 260  # end of hand: cards swept off to the discard tray
CARD_DEAL_ANGLE = 18.0  # degrees of tilt a card carries out of the shoe
CARD_DEAL_START_SCALE = 0.82  # cards leave the shoe slightly small

# A full four-card deal therefore takes 3 * 125 + 200 + 150 = 725ms, dealt in
# table order: player, dealer up-card, player, dealer hole card.

# Where the shoe sits (top-left corner of the drawn stack). Cards fly from its
# centre. Kept clear of the dealer's hand and of the BET readout.
SHOE_POS = (548, 92)
SHOE_SCALE = 0.62

# --- Feedback --------------------------------------------------------------
BANNER_POP_MS = 260  # result banner springs in
BANNER_SHAKE_MS = 380  # ...and shakes instead, on a losing hand
BANNER_SHAKE_PX = 9
COUNTER_ROLL_MS = 260  # CREDITS meter rolls to its new value
FLASH_MS = 380  # brighten-and-fade on a coin in / credit change
BET_BUMP_MS = 220  # BET readout pops when the bet changes
SHUFFLE_NOTICE_MS = 1400  # "SHUFFLING" card slides in and out
PAYOUT_COIN_FALL_MS = 520  # a quarter tumbling out of the dispenser
PAYOUT_COIN_RADIUS = 11
JAM_FLASH_PERIOD_MS = 500  # jam overlay border alternates at this rate
PROMPT_BLINK_MS = 900  # "INSERT QUARTERS" blink period

# --- Attract / title screen ------------------------------------------------
ATTRACT_LETTER_DROP_MS = 420  # each letter of the title falls in
ATTRACT_LETTER_STAGGER_MS = 55
ATTRACT_CARD_SLIDE_MS = 620  # the two demo cards slide in from the wings
ATTRACT_WAVE_PERIOD_MS = 2600  # title letters ripple after they land
ATTRACT_WAVE_PX = 5
ATTRACT_MARQUEE_PX_PER_S = 74  # bottom ticker scroll speed
ATTRACT_PIP_COUNT = 14  # drifting suit pips in the background
ATTRACT_PIP_PERIOD_MS = 9000  # time for one pip to cross the screen
ATTRACT_MARQUEE_TEXT = (
    "BLACKJACK PAYS 3 TO 2   -   DEALER STANDS ON ALL 17   -   "
    "BET 1 TO 4 QUARTERS   -   HIT OR STAND   -   CASH OUT ANY TIME   -   "
)

# --------------------------------------------------------------------------
# Storage durability
# --------------------------------------------------------------------------
#
# How hard the machine works to not forget your money. The expensive part is
# never the writing -- it is fsync, which forces the card to actually commit
# before the game may continue, and which a tired SD card can sit on for
# SECONDS while the whole cabinet appears frozen.
#
#   "durable"  temp file, fsync, rename, fsync the directory. The balance
#              survives the plug being pulled at any instant, including
#              mid-write. This is what a machine taking strangers' money in a
#              public place should run, and it is the only mode in which the
#              crash-reconciliation in bank.py means anything.
#
#   "fast"     the same atomic rename, without the fsyncs. The file is still
#              written and still correct, so credits survive quitting, a crash,
#              and a reboot -- just not power being cut in the split second of
#              a write. Costs microseconds instead of milliseconds, and cannot
#              stall. DEFAULT: right for a machine in your own home.
#
#   "memory"   never touch the disk while playing; the balance is written once
#              on a clean exit. Nothing survives a power cut or a kill -9,
#              including quarters the dispenser still owes you.
#
# The payout reconciliation (resuming an interrupted cash-out) is only
# trustworthy under "durable". Under the other two a power cut during a payout
# can lose the owed count, which for a home machine means you fish your own
# quarters out of the hopper.
# DEFAULT "memory": this cabinet lives in an apartment, its storage is failing,
# and the money it holds is its owner's own. Zero disk writes while playing,
# one on a clean exit. Change to "durable" before it ever takes a coin from
# anybody else.
PERSIST_MODE = "memory"

# The append-only audit trail (state/ledger.log): one line per coin in, bet,
# win and dispensed quarter. Invaluable when a machine that takes strangers'
# money is accused of eating a quarter -- and pointless for a cabinet in your
# own apartment, where it is just writes to the card you did not ask for.
#
# It is not free: it is written on EVERY money event, twice per quarter during
# a cash-out, and it grows forever. On a tired card even these writes stall,
# because the kernel throttles a writer whose dirty pages are not being flushed
# fast enough -- no fsync required. Turn it off if the machine still hitches
# after PERSIST_MODE = "fast".
#
# "memory" mode implies this off: a mode whose whole point is touching no disk
# would be a lie if it still appended a line per event.
LEDGER_ENABLED = True

# --------------------------------------------------------------------------
# Storage health
# --------------------------------------------------------------------------

# Every change to the balance is written atomically: temp file, fsync, rename,
# fsync the directory. That is two full syncs to the SD card, and it BLOCKS the
# game loop -- it has to, because a coin that is only in RAM is a coin the
# machine will forget in a power cut.
#
# A healthy card does this in a few milliseconds. When it starts taking
# hundreds of milliseconds the machine visibly stutters; when it takes seconds,
# the card is failing and the money on it is at risk. Either way the operator
# should be told rather than left guessing, so any write slower than this warns
# on stdout (the journal, under systemd) and leaves a SLOW_WRITE line in the
# ledger.
SLOW_WRITE_WARN_MS = 250

# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------

# Milliseconds between the dealer's cards, so the hand plays out at a
# watchable pace instead of appearing all at once.
DEALER_STEP_MS = 650

# When an animation is still playing the dealer waits for it, then pauses this
# much longer -- drawing a card the instant the hole card finishes flipping
# looks like a glitch rather than a decision.
DEALER_BEAT_MS = 180

# Seconds a finished hand's result stays on screen before returning to idle.
RESULT_DISPLAY_SECONDS = 3.0

# Pressing BET or DEAL on the result screen skips it, on purpose -- an
# impatient player should not have to sit through three seconds. But a button
# press that arrives in the first moments does not mean "skip": on a Pi, saving
# the settled balance to the SD card can block the loop for a few hundred
# milliseconds, and any keypress in that window gets delivered the instant it
# comes back, wiping the result before the player has seen it. Presses this
# soon after the banner appears are ignored.
# 0 restores the old always-skippable behaviour.
RESULT_SKIP_GRACE_MS = 600

# Attract mode after this long with no input (0 disables).
IDLE_ATTRACT_SECONDS = 30.0

# --------------------------------------------------------------------------
# Sound
# --------------------------------------------------------------------------
#
# Every effect is synthesised at startup by audio.py -- there are no WAVs to
# ship, nothing to find at runtime, and nothing that can go missing on the
# cabinet. Set SOUND_ENABLED = False and the machine is silent again, with the
# mixer never opened at all.
#
# If audio hardware is missing or busy the mixer simply fails to open and the
# game carries on without it. Sound is never allowed to stop the machine.
SOUND_ENABLED = True

# Master level applied when the sounds are rendered, 0.0-1.0. The individual
# effects are balanced against each other inside audio.py; change this rather
# than those, or they stop being balanced.
SOUND_VOLUME = 0.7

# Mixer buffer in samples. Small = responsive, which is what an arcade button
# needs; too small and a busy Pi underruns and crackles. 512 at 22.05kHz is
# about 23ms of latency. Raise it to 1024 if you hear crackling.
SOUND_BUFFER = 512

# Simultaneous sounds. Four cards can be in the air at once, over a win jingle.
SOUND_CHANNELS = 8

# Don't retrigger the card-landing click more often than this. Four cards land
# within 500ms of each other on a deal, and with animations disabled they land
# in the SAME frame -- four identical waveforms in phase is not four times as
# loud, it is a clipped bang.
SOUND_CARD_MIN_GAP_MS = 40
