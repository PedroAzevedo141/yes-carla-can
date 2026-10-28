"""Small, exportable experiment report; rendering stays outside timing windows."""
import json

import cv2
import numpy as np


def save_preview(image, detections, names, path):
    rgb = image.squeeze(0).detach().cpu().permute(1, 2, 0).numpy()
    bgr = np.ascontiguousarray(np.rint(rgb[:, :, ::-1] * 255).clip(0, 255).astype(np.uint8))
    shown = detections[:40].tolist()
    legends = []
    palette = [(80, 215, 100), (235, 170, 80), (150, 120, 240), (100, 220, 235)]
    for index, (x1, y1, x2, y2, score, cls) in enumerate(shown):
        x1, y1, x2, y2 = [max(0, min(639, round(v))) for v in [x1, y1, x2, y2]]
        cls = int(cls)
        label = names.get(cls, str(cls)) if isinstance(names, dict) else names[cls]
        color = palette[index % len(palette)]
        cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 1)
        if index < 12:
            cv2.putText(bgr, str(index + 1), (x1, max(y1 - 4, 12)), cv2.FONT_HERSHEY_SIMPLEX, .42, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(bgr, str(index + 1), (x1, max(y1 - 4, 12)), cv2.FONT_HERSHEY_SIMPLEX, .42, color, 1, cv2.LINE_AA)
            legends.append((f"{index + 1}. {label}  {score:.2f}", color))
    header = 38
    footer = max(1, (len(legends) + 1) // 2) * 24 + 20
    canvas = np.full((640 + header + footer, 640, 3), 28, dtype=np.uint8)
    canvas[header:header + 640] = bgr
    cv2.putText(canvas, f"YOLOv5s | 640x640 | {len(shown)}/{len(detections)} boxes shown", (12, 25), cv2.FONT_HERSHEY_SIMPLEX, .50, (245, 245, 245), 1, cv2.LINE_AA)
    for index, (label, color) in enumerate(legends):
        position = (12 + (index % 2) * 320, header + 640 + 24 + (index // 2) * 24)
        cv2.putText(canvas, label, position, cv2.FONT_HERSHEY_SIMPLEX, .46, color, 1, cv2.LINE_AA)
    bgr = canvas
    if not cv2.imwrite(str(path), bgr):
        raise OSError(f"Falha ao salvar {path}")


def summarize(rows, budget_ms):
    summary = {}
    for name in dict.fromkeys(r["condition"] for r in rows):
        selected = [r for r in rows if r["condition"] == name]
        summary[name] = {"samples": len(selected), "unique_frames": len({r["frame"] for r in selected})}
        for metric in ["qualified_proposals", "expanded_candidates", "nms_input", "detections", "inference_ms", "nms_ms", "postprocess_ms", "detector_ms"]:
            values = np.array([r[metric] for r in selected])
            summary[name][metric] = {"mean": float(values.mean()), "median": float(np.median(values)), "p95": float(np.percentile(values, 95))}
        total = sum(r["clean_reference"] for r in selected)
        summary[name]["preservation_micro"] = sum(r["matched_clean"] for r in selected) / total if total else None
        summary[name]["budget_exceeded_fraction"] = sum(r["detector_ms"] > budget_ms for r in selected) / len(selected)
    return summary


def write_report(out, rows, budget_ms, metadata):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    summary = summarize(rows, budget_ms)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    order = [n for n in ["clean", "random", "attack"] if n in summary]
    labels = {"clean": "Limpa", "random": "Ruido", "attack": "Perturbacao"}
    colors = {"clean": "#268e89", "random": "#c19445", "attack": "#bd5366"}
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.4), layout="constrained")
    for ax, (metric, title) in zip(axes, [("nms_input", "Candidatos ao NMS"), ("nms_ms", "NMS (ms)"), ("detector_ms", "Detector + pos-processamento (ms)")]):
        values = [summary[n][metric]["median"] for n in order]
        bars = ax.bar([labels[n] for n in order], values, color=[colors[n] for n in order], width=.55)
        ax.bar_label(bars, fmt="%.3f" if metric.endswith("ms") else "%.1f", padding=4, fontsize=9)
        ax.set_ylim(0, max(max(values) * 1.25, .001))
        ax.set_title(title, fontsize=10)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_axisbelow(True)
        ax.grid(axis="y", alpha=.16)
    fig.suptitle("YOLOv5s | mediana das medicoes | " + str(metadata["environment"]["gpu"] or "CPU"), fontsize=11)
    fig.savefig(out / "comparison.png", dpi=180)
    plt.close(fig)
    lines = ["# Experimento de percepção — Yes, CARLA CAN", "",
             "Medição offline com YOLOv5s, entrada RGB 640×640, FP32 e lote de uma imagem.", "",
             "| Condição | Frames distintos | Medições | Candidatos (média) | Inferência (mediana ms) | NMS (mediana ms) | Total do detector (mediana ms) | Preservação |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for n in order:
        s = summary[n]
        preservation = f"{100*s['preservation_micro']:.1f}%" if s["preservation_micro"] is not None else "Indefinida"
        lines.append(f"| {labels[n]} | {s['unique_frames']} | {s['samples']} | {s['nms_input']['mean']:.2f} | {s['inference_ms']['median']:.3f} | {s['nms_ms']['median']:.3f} | {s['detector_ms']['median']:.3f} | {preservation} |")
    lines += ["", "![Comparação](comparison.png)", "", "## Como interpretar", "",
              "Repetições medem variabilidade de tempo; não são novas cenas independentes. Preservação compara previsões limpas por classe e IoU, sem anotações de verdade de referência.", "",
              "Inferência e NMS usam relógio de parede com sincronização CUDA. O tempo NMS inclui chamada, seleção e reunião das caixas; o total do detector inclui inferência, filtragem e NMS. Leitura, redimensionamento, envio à GPU, aplicação da perturbação, desenhos e gravações ficam fora desse total.", "",
              f"O orçamento de {budget_ms:.3f} ms é uma referência de processamento escolhida para o experimento. Os dados não medem idade do frame na rede, controle do veículo ou garantia de segurança.", "",
              "O ambiente e os hashes dos arquivos estão em `manifest.json`; os valores individuais estão em `frames.csv`. As imagens anotadas usam o espaço 640×640 do detector e ficam em `previews/`."]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return summary
