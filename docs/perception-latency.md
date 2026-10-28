# Percepção visual e reprodução do Groundswell

O módulo opcional `perception_latency` acrescenta detecção de objetos e medição de latência ao Yes, CARLA CAN. O primeiro marco é a referência **sem ataque**, usando YOLOv5s em imagens gravadas do CARLA. O mesmo consumidor permite comparar imagem limpa, ruído e perturbação treinada.

O [painel ao vivo](perception-live.md) permite acompanhar a câmera e alternar a perturbação na tela. Os [resultados iniciais](perception-results.md) registram os experimentos offline.

## O que está sendo medido

```text
Imagem RGB → redimensionamento 640×640 → YOLOv5s → filtragem → NMS → detecções
```

O detector produz caixas candidatas. O NMS elimina caixas sobrepostas. Registramos candidatos, tempo da inferência, tempo do NMS e tempo total do detector, com sincronização da GPU. A preparação da imagem, os desenhos e as gravações são registrados ou executados fora desse total. O tempo de NMS inclui a chamada, seleção e reunião das caixas; o pós-processamento inclui também filtragem e expansão por classe.

O redimensionamento quadrado segue o artefato dos autores, em vez de adicionar bordas. Os desenhos salvos representam a entrada 640×640 efetivamente processada. As métricas não incluem a idade de um frame na rede nem consequências na direção ou frenagem.

## Ambiente e modelo

Na raiz deste repositório, use o ambiente Python do projeto:

```bash
conda activate n4s_env
python scripts/fetch_groundswell.py
./scripts/run_perception.sh doctor --load-model
```

O launcher também reconhece a instalação habitual `~/miniconda3/envs/n4s_env`. Para selecionar outro Python:

```bash
PERCEPTION_PYTHON=/caminho/do/ambiente/bin/python ./scripts/run_perception.sh doctor
```

As dependências opcionais testadas estão em `requirements-perception.txt`. Para uma instalação nova, use um ambiente separado e instale esse arquivo. O instalador principal do projeto não foi alterado. Dados, pesos, código externo, caches e resultados ficam em `.perception/`, ignorado pelo Git. O download verifica o SHA-256 do ZIP e não executa instaladores externos.

O adaptador usa o YOLOv5s e os pesos do [artefato citado no apêndice, registro 20397573](https://zenodo.org/records/20397573), fixados por hash. O ZIP inclui imagens BDD100K e rótulos para experimentos de referência, além do código e pesos. [Artigo](https://www.weisongshi.org/papers/xia26-latencyattack.pdf).

Este ambiente usa Python 3.9 e PyTorch 2.8, enquanto o apêndice descreve Python 3.8 e PyTorch 1.10.1. Trata-se de uma adaptação para a máquina local, com versões e hashes registrados em cada execução. Comparar os tempos diretamente com a tabela do artigo exige considerar essa diferença.

## Primeiro experimento: sem ataque

O repositório já contém `output_stream.ts`. Para extrair um piloto:

```bash
./scripts/run_perception.sh extract \
  --source output_stream.ts \
  --output .perception/datasets/carla-pilot \
  --stride 12

./scripts/run_perception.sh baseline \
  --images .perception/datasets/carla-pilot/test \
  --output .perception/runs/baseline \
  --repeats 10
```

Escolha uma nova pasta de saída para repetir comandos: resultados existentes não são sobrescritos. A gravação curta é material de validação funcional. Ela contém avisos de decodificação H.264; exceções e frames marcados como corrompidos são contados, mas esses contadores não cobrem todos os avisos de recuperação do decodificador.

O extrator separa os primeiros 60% dos frames amostrados para treino, os 20% intermediários como intervalo temporal e os últimos 20% para teste. Isso evita reutilizar exatamente os mesmos frames, mas **não cria cenas independentes**. Para avaliação científica, capture percursos distintos para treino e teste. Repetições de uma imagem medem variação do tempo; não aumentam o número de cenas.

Cada execução gera:

- `report.md`: explicação e tabela das medições.
- `frames.csv`: valores por imagem e repetição.
- `summary.json`: média, mediana e percentil 95.
- `manifest.json`: ambiente, parâmetros e hashes dos arquivos.
- `previews/`: algumas imagens com as detecções desenhadas.
- `comparison.png`: gráfico para inspeção dos resultados.

## Capturar novas imagens do CARLA

Ative a captura ao iniciar o cliente pelo fluxo normal do projeto:

```bash
YES_CARLA_CAPTURE_DIR="$PWD/.perception/captures" \
YES_CARLA_CAPTURE_STRIDE=15 \
python CARLA_client_module.py
```

Cada instância da câmera cria uma subpasta de sessão com identificador próprio, contendo PNGs limpos e timestamps do CARLA. A gravação usa uma fila limitada e uma thread para não bloquear a câmera com escrita em disco. O arquivo `capture_status.txt` registra descartes por fila cheia, pendências e erros ao encerrar. Reinicializar a câmera cria outra sessão.

Sem essas variáveis, o módulo de captura não é inicializado. A inferência ocorre em um consumidor separado; não é executada dentro da callback da câmera.

## Reprodução adaptada do ataque

Depois de estabelecer a referência:

```bash
./scripts/run_perception.sh train \
  --images .perception/datasets/carla-pilot/train \
  --output .perception/runs/groundswell-r04 \
  --ratio 0.4 --epochs 100

./scripts/run_perception.sh evaluate \
  --images .perception/datasets/carla-pilot/test \
  --delta .perception/runs/groundswell-r04/delta.npy \
  --output .perception/runs/comparison-r04 \
  --repeats 10
```

O treinamento chama as funções de perda, gradiente e atualização regional do código dos autores. O detector fica fixo: aprende-se somente a perturbação. Os parâmetros padrão incluem raio L2 70, passo 0,0008, peso de área 10 e lote 8. O artigo principal menciona passo 0,0005; o apêndice usa 0,0008. O `run_attack.py` da v1 tem outros padrões, incluindo peso de área zero. Nosso manifesto registra a escolha efetivamente usada.

Adaptações explícitas: entrada de imagens sem necessidade de rótulos, leitura sob demanda, épocas fixas sem o early stopping original, separação do grafo entre passos e proteção para norma zero. A máscara conserva o comportamento do artefato 20397573, com atualização efetiva por época e perturbação aditiva não negativa. Com razão zero, a versão atual libera a imagem inteira para a perturbação; a versão inicial não tinha esse caso especial. O resultado float32 é salvo como `delta.npy`, evitando perda de precisão ao salvar apenas PNG.

A comparação inclui um padrão aleatório com mesmo suporte espacial e norma L2. A ordem das condições é alternada de forma reproduzível. Arquivos com hashes presentes no manifesto de treino são rejeitados como teste. Preservação usa classe e IoU com as previsões limpas, sem representar recall contra anotações reais; quando não há detecções limpas, a preservação fica indefinida.

## Amostra de referência BDD100K

O artefato atualizado contém imagens em `.perception/artifacts/RPBAttack/val`. Para um piloto separado das cenas do CARLA:

```bash
./scripts/run_perception.sh split \
  --images .perception/artifacts/RPBAttack/val \
  --output .perception/datasets/bdd-pilot \
  --train-count 160 --test-count 30 --seed 42
```

Use as pastas `train` e `test` nos comandos de treinamento e avaliação. A amostragem não repete arquivos e rejeita hashes duplicados. Um subconjunto de 190 imagens valida o fluxo, mas não equivale à avaliação completa dos autores nem demonstra transferência para o CARLA.

## Aplicação antes da compressão

Para um experimento posterior no caminho da câmera:

```bash
YES_CARLA_DELTA="$PWD/.perception/runs/groundswell-r04/delta.npy" \
python CARLA_client_module.py
```

A câmera redimensiona o delta RGB para sua resolução, aplica-o aos pixels e depois codifica e transmite o vídeo existente. Isso altera a geometria e a norma efetiva em relação à entrada quadrada: não presumir que o efeito medido offline sobreviva à conversão e à compressão. A captura opcional continua salvando a entrada limpa. Este ponto de integração precisa de validação com o simulador e a transmissão ativos; testes isolados não equivalem a essa validação.

## Testes

```bash
python -m pytest tests/ -q
```

Os testes verificam equivalência do pós-processamento com o código do artefato, limite de candidatos, correspondência das detecções, validação do delta, ordem RGB/BGRA e captura da imagem limpa. O teste de equivalência requer o artefato; é marcado como ignorado quando ele não está instalado. Nenhum teste exige servidor CARLA ou GPU.

O código externo inclui componentes YOLOv5 com suas próprias licenças e créditos. Ele permanece separado em `.perception/artifacts/`; os arquivos originais e seus avisos são preservados.
