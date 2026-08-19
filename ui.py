"""
pygame rendering and keyboard input.

This is the ONLY file that imports pygame. It draws the game state it is handed
and pushes keystrokes into the hardware backend; it holds no game or money
state of its own.

Everything here is built for composite video on a 480-line CRT: chunky fonts,
fat strokes, no 1px detail, and every pixel that matters kept inside the
overscan-safe rectangle.
"""

from __future__ import annotations

import pygame

import config
from cards import Card
from game import BlackjackGame, Phase


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


def play_sound(name: str) -> None:
    """SOUND HOOK -- deliberately a no-op; audio is out of scope for now.

    Call sites already exist (coin accepted, card dealt, win, jam). To make the
    machine noisy: init pygame.mixer in UI.__init__, load WAVs into a dict here,
    and flip config.SOUND_ENABLED.
    """
    if not config.SOUND_ENABLED:
        return


class UI:
    def __init__(self, fullscreen: bool = False) -> None:
        pygame.init()
        pygame.display.set_caption("Blackjack")
        pygame.mouse.set_visible(False)  # it's a cabinet, there's no mouse

        self.fullscreen = fullscreen
        self.screen = self._make_screen(fullscreen)
        self.clock = pygame.time.Clock()
        self.show_safe_guide = config.SHOW_SAFE_AREA_GUIDE

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
        self.key_quit = pygame.key.key_code(config.KEY_QUIT)

    def _make_screen(self, fullscreen: bool) -> pygame.Surface:
        flags = pygame.SCALED  # letterbox if the framebuffer isn't 640x480
        if fullscreen:
            flags |= pygame.FULLSCREEN
        return pygame.display.set_mode(
            (config.SCREEN_WIDTH, config.SCREEN_HEIGHT), flags
        )

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        self.screen = self._make_screen(self.fullscreen)

    def quit(self) -> None:
        pygame.quit()

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
        return True

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

    def _draw_suit(
        self,
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
        half = size // 2
        quarter = max(2, size // 4)

        if suit == "D":
            pygame.draw.polygon(
                self.screen,
                color,
                [(cx, cy - half), (cx + half, cy), (cx, cy + half), (cx - half, cy)],
            )
        elif suit == "H":
            pygame.draw.circle(self.screen, color, (cx - quarter, cy - quarter), quarter)
            pygame.draw.circle(self.screen, color, (cx + quarter, cy - quarter), quarter)
            pygame.draw.polygon(
                self.screen,
                color,
                [(cx - half, cy - quarter), (cx + half, cy - quarter), (cx, cy + half)],
            )
        elif suit == "S":
            pygame.draw.polygon(
                self.screen,
                color,
                [(cx, cy - half), (cx - half, cy + quarter), (cx + half, cy + quarter)],
            )
            pygame.draw.circle(self.screen, color, (cx - quarter, cy + quarter), quarter)
            pygame.draw.circle(self.screen, color, (cx + quarter, cy + quarter), quarter)
            pygame.draw.rect(
                self.screen, color, (cx - quarter // 2, cy + quarter, quarter, half // 2)
            )
        else:  # clubs
            pygame.draw.circle(self.screen, color, (cx, cy - quarter), quarter)
            pygame.draw.circle(self.screen, color, (cx - quarter, cy + quarter // 2), quarter)
            pygame.draw.circle(self.screen, color, (cx + quarter, cy + quarter // 2), quarter)
            pygame.draw.rect(
                self.screen, color, (cx - quarter // 2, cy + quarter, quarter, half // 2)
            )

    def _draw_card(self, card: Card | None, x: int, y: int) -> None:
        rect = pygame.Rect(x, y, config.CARD_WIDTH, config.CARD_HEIGHT)

        if card is None:  # face down
            pygame.draw.rect(self.screen, config.COLOR_CARD_BACK, rect, border_radius=6)
            pygame.draw.rect(
                self.screen,
                config.COLOR_TEXT,
                rect,
                width=config.MIN_LINE_WIDTH,
                border_radius=6,
            )
            inner = rect.inflate(-18, -22)
            pygame.draw.rect(
                self.screen, config.COLOR_TEXT_DIM, inner, width=config.MIN_LINE_WIDTH
            )
            return

        pygame.draw.rect(self.screen, config.COLOR_CARD_FACE, rect, border_radius=6)
        pygame.draw.rect(
            self.screen,
            config.COLOR_CARD_BLACK,
            rect,
            width=config.MIN_LINE_WIDTH,
            border_radius=6,
        )

        ink = config.COLOR_CARD_RED if card.is_red else config.COLOR_CARD_BLACK
        label = self.font_medium.render(card.rank, True, ink)
        self.screen.blit(label, (rect.left + 6, rect.top + 4))
        self._draw_suit(card.suit, (rect.centerx, rect.centery + 14), 30, ink)

    def _draw_hand(self, cards: list[Card | None], top: int) -> None:
        if not cards:
            return
        spread = config.CARD_WIDTH + 8
        area_width = config.SAFE_RECT[2]
        if len(cards) * spread > area_width:
            spread = config.CARD_SPACING  # fan them out overlapping instead
        total = spread * (len(cards) - 1) + config.CARD_WIDTH
        x = (config.SCREEN_WIDTH - total) // 2
        for card in cards:
            self._draw_card(card, x, top)
            x += spread

    # ------------------------------------------------------------------
    # Frame
    # ------------------------------------------------------------------

    def render(self, game: BlackjackGame, bank, payout, notice: str | None = None) -> None:
        self.screen.fill(config.COLOR_BG)

        safe_x, safe_y, safe_w, safe_h = config.SAFE_RECT
        safe_right = safe_x + safe_w
        centre_x = config.SCREEN_WIDTH // 2

        # --- top bar: the two numbers that are always true --------------
        self._text(
            f"CREDITS {bank.balance_quarters}",
            self.font_large,
            config.COLOR_ACCENT,
            left=safe_x,
            top=safe_y,
        )
        self._text(
            f"BET {game.bet_quarters}",
            self.font_large,
            config.COLOR_ACCENT,
            right=safe_right,
            top=safe_y,
        )

        # --- dealer -----------------------------------------------------
        dealer_label = "DEALER"
        if game.dealer_cards:
            if game.dealer_hole_hidden:
                dealer_label = f"DEALER  {game.dealer_visible_total}+"
            else:
                dealer_label = f"DEALER  {game.dealer_visible_total}"
        self._text(
            dealer_label,
            self.font_small,
            config.COLOR_TEXT_DIM,
            left=safe_x,
            top=ROW_DEALER_LABEL,
        )

        dealer_row: list[Card | None] = list(game.dealer_visible_cards)
        if game.dealer_hole_hidden and len(game.dealer_cards) > 1:
            dealer_row.append(None)  # the hole card
        self._draw_hand(dealer_row, ROW_DEALER_CARDS)

        # --- player -----------------------------------------------------
        player_label = "YOU"
        if game.player_cards:
            soft = "/soft" if game.player_is_soft else ""
            player_label = f"YOU  {game.player_total}{soft}"
        self._text(
            player_label,
            self.font_small,
            config.COLOR_TEXT_DIM,
            left=safe_x,
            top=ROW_PLAYER_LABEL,
        )
        self._draw_hand(list(game.player_cards), ROW_PLAYER_CARDS)

        # --- banner and prompt ------------------------------------------
        banner, banner_color = self._banner_for(game, bank, payout)
        if banner:
            self._text(banner, self.font_huge, banner_color, center_x=centre_x, top=ROW_BANNER)

        prompt = notice or self._prompt_for(game, bank, payout)
        if prompt:
            self._text(prompt, self.font_medium, config.COLOR_TEXT, center_x=centre_x, top=ROW_PROMPT)

        # --- footer hints ------------------------------------------------
        self._text(
            self._hints_for(game, bank, payout),
            self.font_small,
            config.COLOR_TEXT_DIM,
            center_x=centre_x,
            top=ROW_HINTS,
        )

        # A jam is the one thing that must not be missable.
        if payout.status.jammed:
            self._draw_jam_overlay(bank)

        if self.show_safe_guide:
            pygame.draw.rect(
                self.screen, (255, 0, 255), pygame.Rect(*config.SAFE_RECT), width=2
            )

        pygame.display.flip()
        self.clock.tick(config.FPS)

    def _banner_for(self, game, bank, payout):
        if payout.status.jammed:
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

    def _draw_jam_overlay(self, bank) -> None:
        box = pygame.Rect(config.SAFE_RECT[0], 150, config.SAFE_RECT[2], 180)
        pygame.draw.rect(self.screen, config.COLOR_ALERT_BG, box)
        pygame.draw.rect(
            self.screen, config.COLOR_TEXT, box, width=config.MIN_LINE_WIDTH
        )
        centre_x = config.SCREEN_WIDTH // 2
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
