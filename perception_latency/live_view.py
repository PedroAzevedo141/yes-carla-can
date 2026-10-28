"""Pygame presentation only. Inference and decoding never run in this loop."""
import time
from collections import deque

import cv2
import numpy as np

LABELS = {"clean": "Limpa", "attack": "Ataque", "random": "Ruido"}
BG, PANEL, TEXT, MUTED = (15, 22, 31), (24, 35, 47), (231, 238, 244), (150, 169, 184)
CYAN, RED, GOLD = (62, 192, 186), (232, 104, 125), (225, 181, 85)


class LiveView:
    def __init__(self, headless=False):
        import pygame
        self.pg, self.headless = pygame, headless
        pygame.font.init()
        self.width, self.height = 1280, 760
        if headless:
            self.screen = pygame.Surface((self.width, self.height))
        else:
            pygame.display.init()
            info = pygame.display.Info()
            self.width = min(self.width, max(960, info.current_w - 60))
            self.height = min(self.height, max(680, info.current_h - 90))
            self.screen = pygame.display.set_mode((self.width, self.height))
            pygame.display.set_caption("Yes, CARLA CAN | Percepcao ao vivo")
        self.font = pygame.font.SysFont("DejaVu Sans", 16)
        self.small = pygame.font.SysFont("DejaVu Sans", 13)
        self.title = pygame.font.SysFont("DejaVu Sans", 25, bold=True)
        self.large = pygame.font.SysFont("DejaVu Sans", 27, bold=True)
        self.clock = pygame.time.Clock()
        self.buttons = {mode: pygame.Rect(18 + i * 140, 84, 126, 36) for i, mode in enumerate(LABELS)}
        self.history = deque(maxlen=150)
        self.timestamps = deque(maxlen=60)
        self.last_key = None
        self.cached = {}
        self.last_generation = None
        self.screenshot_requested = False

    def text(self, text, xy, color=TEXT, font=None):
        self.screen.blit((font or self.font).render(str(text), True, color), xy)

    def events(self, control):
        if self.headless:
            return True
        pg = self.pg
        for event in pg.event.get():
            if event.type == pg.QUIT:
                return False
            if event.type == pg.KEYDOWN:
                if event.key in (pg.K_ESCAPE, pg.K_q):
                    return False
                if event.key == pg.K_SPACE:
                    control.toggle()
                elif event.key in (pg.K_a, pg.K_1):
                    control.select("attack")
                elif event.key in (pg.K_c, pg.K_0):
                    control.select("clean")
                elif event.key == pg.K_r:
                    control.select("random")
                elif event.key == pg.K_s:
                    self.screenshot_requested = True
            if event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
                for mode, rect in self.buttons.items():
                    if rect.collidepoint(event.pos):
                        control.select(mode)
        return True

    def picture(self, rgb, boxes, size, color):
        image = cv2.resize(rgb, size, interpolation=cv2.INTER_LINEAR)
        sx, sy = size[0] / 640, size[1] / 640
        for i, (x1, y1, x2, y2, score, cls) in enumerate(boxes[:40].tolist()):
            x1, x2 = [max(0, min(size[0] - 1, round(v * sx))) for v in (x1, x2)]
            y1, y2 = [max(0, min(size[1] - 1, round(v * sy))) for v in (y1, y2)]
            cv2.rectangle(image, (x1, y1), (x2, y2), color, 1)
            if i < 8:
                cv2.putText(image, str(i + 1), (x1, max(12, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, .4, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(image, str(i + 1), (x1, max(12, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, .4, color, 1, cv2.LINE_AA)
        return self.pg.image.frombuffer(np.ascontiguousarray(image).tobytes(), size, "RGB").copy()

    def draw(self, source, mode, result, status, processed, dropped, automatic):
        pg = self.pg
        active_color = RED if mode == "attack" else GOLD if mode == "random" else CYAN
        self.screen.fill(BG)
        self.text("Yes, CARLA CAN  |  Percepcao ao vivo", (18, 13), font=self.title)
        self.text(source, (20, 51), MUTED)
        self.text("Mesmo frame, duas condicoes", (self.width - 270, 51), MUTED, self.small)
        for name, rect in self.buttons.items():
            color = active_color if name == mode else PANEL
            pg.draw.rect(self.screen, color, rect, border_radius=6)
            self.text(LABELS[name], (rect.x + 18, rect.y + 7), BG if name == mode else TEXT)
        hint = "Espaco: alternar  |  C: limpa  A: ataque  R: ruido  S: foto  Esc: sair"
        self.text(hint, (454, 94), MUTED, self.small)
        if automatic:
            self.text("Alternancia automatica ativa", (20, 124), GOLD, self.small)
        else:
            self.text("Perturbacao digital aplicada apos a decodificacao, antes do detector", (20, 124), MUTED, self.small)
        margin, gap = 18, 16
        pw = (self.width - 2 * margin - gap) // 2
        ph = min(int(pw / 2.4), max(180, self.height - 480))
        top = 151
        metric_y = top + 27 + ph + 12
        if result is not None:
            key = (result["frame_id"], result["generation"])
            if key != self.last_key:
                if result["generation"] != self.last_generation:
                    self.history.clear()
                    self.last_generation = result["generation"]
                self.history.append((result["clean"]["nms_ms"], result["active"]["nms_ms"]))
                self.timestamps.append(result["completed_at"])
                self.cached = {
                    "clean": self.picture(result["clean_rgb"], result["clean_boxes"], (pw, ph), CYAN),
                    "active": self.picture(result["active_rgb"], result["active_boxes"], (pw, ph), active_color),
                }
                self.last_key = key
        for i, name in enumerate(["clean", "active"]):
            x = margin + i * (pw + gap)
            title = "Referencia limpa" if i == 0 else "Condicao: " + LABELS[mode]
            color = CYAN if i == 0 else active_color
            self.text(title, (x + 2, top), color)
            pg.draw.rect(self.screen, PANEL, (x, top + 27, pw, ph), border_radius=5)
            if result is not None:
                self.screen.blit(self.cached[name], (x, top + 27))
            else:
                self.text(status if not processed else "Aplicando nova condicao...", (x + 18, top + 55), MUTED)
            pg.draw.rect(self.screen, PANEL, (x, metric_y, pw, 145), border_radius=7)
            if result is not None:
                metrics = result[name]
                self.text(f"NMS  {metrics['nms_ms']:.2f} ms", (x + 14, metric_y + 9), color, self.large)
                self.text(f"Detector completo   {metrics['detector_ms']:.2f} ms", (x + 14, metric_y + 49))
                self.text(f"Candidatos ao NMS   {metrics['nms_input']:,}", (x + 14, metric_y + 79))
                self.text(f"Deteccoes   {metrics['detections']:,}  (ate 40 caixas desenhadas)", (x + 14, metric_y + 109), MUTED, self.small)
        footer_y = metric_y + 157
        fps = (len(self.timestamps) - 1) / (self.timestamps[-1] - self.timestamps[0]) if len(self.timestamps) > 1 and self.timestamps[-1] > self.timestamps[0] else 0
        self.text(f"Resultados: {fps:.1f}/s   |   Frames substituidos: {dropped}   |   Processados: {processed}", (20, footer_y), MUTED, self.small)
        if result is not None:
            retained = "indefinidas" if result["preservation"] is None else f"{100 * result['preservation']:.1f}%"
            age = (time.perf_counter() - result["arrived_at"]) * 1000
            self.text(f"Deteccoes mantidas: {retained}   |   Idade local apos chegada: {age:.0f} ms", (20, footer_y + 23), TEXT, self.small)
        gy, gh = footer_y + 57, self.height - footer_y - 79
        if gh > 20:
            pg.draw.rect(self.screen, PANEL, (18, gy, self.width - 36, gh), border_radius=5)
            self.text("NMS por frame  |  referencia / condicao", (30, gy + 5), MUTED, self.small)
            if len(self.history) > 1:
                ceiling = max(.2, max(max(pair) for pair in self.history) * 1.15)
                for index, color in enumerate([CYAN, active_color]):
                    points = [(30 + i * (self.width - 64) / (len(self.history) - 1), gy + gh - 5 - (pair[index] / ceiling) * max(5, gh - 29)) for i, pair in enumerate(self.history)]
                    pg.draw.lines(self.screen, color, False, points, 2)
        if not self.headless:
            pg.display.flip()

    def tick(self):
        self.clock.tick(30)

    def save(self, path):
        self.pg.image.save(self.screen, str(path))

    def close(self):
        if not self.headless:
            self.pg.display.quit()
        self.pg.font.quit()
