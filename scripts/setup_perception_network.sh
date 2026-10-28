#!/usr/bin/env bash
# Prepare the existing YCC topology without deleting a pre-existing network.
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
task_check=0
if [[ "${1:-}" == --check && $# == 1 ]]; then
    task_check=1
elif [[ $# != 0 ]]; then
    echo "Uso: $0 [--check]" >&2
    exit 2
fi
[[ $EUID == 0 ]] || { echo 'Execute este preparador com sudo.' >&2; exit 1; }

task_present=0
for task_namespace in sender switch receiver; do
    if [[ -e "/run/netns/$task_namespace" ]]; then
        task_present=$((task_present + 1))
    fi
done
if [[ $task_present == 0 && $task_check == 0 ]]; then
    for task_interface in veth-s veth-s-sw veth-r veth-r-sw; do
        if ip link show dev "$task_interface" &>/dev/null; then
            echo "Interface $task_interface ja existe fora da topologia esperada; nenhuma rede foi apagada." >&2
            exit 1
        fi
    done
    bash "$task_root/avtp_network/setup.sh"
elif [[ $task_present != 3 ]]; then
    echo 'Topologia AVTP ausente ou incompleta. Nenhuma rede existente foi apagada.' >&2
    exit 1
fi

# The normal YCC camera runs on the host; only the receiver lives in a netns.
if ! ip link show dev veth-s &>/dev/null; then
    if [[ $task_check == 0 ]] && ip -n sender link show dev veth-s &>/dev/null; then
        ip netns exec sender ip link set veth-s netns "$$"
    else
        echo 'A interface veth-s deve estar no host para a camera do YCC.' >&2
        exit 1
    fi
fi
ip -n receiver link show dev veth-r >/dev/null
ip -n switch link show dev br0 >/dev/null
for task_interface in veth-s-sw veth-r-sw; do
    task_master=$(ip -n switch -o link show dev "$task_interface")
    [[ " $task_master " == *' master br0 '* ]] || {
        echo "$task_interface nao pertence a br0; topologia existente preservada." >&2; exit 1;
    }
done
if [[ $task_check == 0 ]]; then
    ip link set dev veth-s up
    ip -n receiver link set veth-r up
    ip -n switch link set br0 up
    ip -n switch link set veth-s-sw up
    ip -n switch link set veth-r-sw up
    modprobe vcan
    if ! ip link show dev vcan0 &>/dev/null; then
        ip link add dev vcan0 type vcan
    fi
    task_can=$(ip -d -o link show dev vcan0)
    [[ " $task_can " == *' vcan '* ]] || { echo 'vcan0 nao e uma interface CAN virtual.' >&2; exit 1; }
    ip link set dev vcan0 up
fi
task_can=$(ip -d -o link show dev vcan0)
[[ " $task_can " == *' vcan '* ]] || { echo 'vcan0 nao e uma interface CAN virtual.' >&2; exit 1; }
check_up() {
    local task_link
    task_link=$("$@")
    [[ "$task_link" == *'<UP,'* || "$task_link" == *',UP,'* || "$task_link" == *',UP>'* ]] || {
        echo "Interface desligada: $task_link" >&2; exit 1;
    }
}
check_up ip -o link show dev veth-s
check_up ip -n receiver -o link show dev veth-r
check_up ip -n switch -o link show dev br0
check_up ip -n switch -o link show dev veth-s-sw
check_up ip -n switch -o link show dev veth-r-sw
check_up ip -o link show dev vcan0
echo 'Rede pronta: camera no host/veth-s -> switch/br0 -> receiver/veth-r; CAN em vcan0.'
echo 'A rede sera preservada ao fechar o painel. Nao foi criado um barramento de ataque CAN.'
