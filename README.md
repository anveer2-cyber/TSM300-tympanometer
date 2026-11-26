# TSM300-tympanometer

Oscilla TSM-300 임피던스 청력검사기(tympanometer)의 검사 결과를 검사 직후 PC로 가져오는 프로그램입니다.

## 목표

1. 검사 후 장비에서 `->` 키를 누르면 PC가 결과를 바로 받는다.
2. 받은 결과(고막운동도 곡선, 중이 압력, 외이도 용적, 컴플라이언스, 폭, 등골근 반사)를 해독한다.
3. 예진이텔레그램의 검사실 정보에서 환자를 확인하고, 결과를 상쾌한캡처로 보낸다.

## 현재 단계: 통신 캡처

TSM-300의 시리얼 통신 규격은 공개되어 있지 않습니다. 그래서 먼저 장비가 실제로 보내는 데이터를 받아 저장하는 캡처 도구를 만들었습니다. 이 캡처를 분석해 해독기를 만듭니다.

### 설치와 실행 (Windows, Python 3.9 이상)

```bash
python -m pip install --upgrade https://github.com/anveer2-cyber/TSM300-tympanometer/archive/refs/heads/claude/cool-hamilton-bdp178.zip
```

```bash
python -m tsm300
```

`python` 명령이 없다고 나오면 `py -m pip ...`, `py -m tsm300` 으로 실행합니다.

### 장비 쪽 준비

- `MENU` -> `Right key` -> `TRANSMIT`
- `MENU` -> `Printer setup` -> `Printer Port` -> `Parallel` (Serial 이면 장비가 PC로 식별 신호를 보내지 않습니다)
- 장비 뒷면 `SERIAL` 단자와 PC를 케이블로 연결

### 진행 순서

1. 프로그램이 포트를 찾습니다. 여러 개면 번호로 고릅니다.
2. 캡처가 시작되면 검사 결과 화면에서 `->` 키를 누릅니다. 받은 데이터가 화면에 표시되고, 메모 칸에 장비 화면의 값을 적습니다.
3. 메모 칸에 `q` 를 입력하면 끝나고, `captures` 폴더에 결과 폴더와 zip 파일이 만들어집니다.

통신 속도는 실제 장비에서 확인한 9600 bps 가 기본값입니다. `python -m tsm300 --port COM5` 처럼 포트를 바로 지정할 수 있고, `python -m tsm300 --list` 는 포트 목록만 보여 줍니다. 다른 장비라서 속도를 모를 때는 `python -m tsm300 --scan` 으로 찾습니다. 장비가 켜질 때 보내는 식별 신호로 속도를 판별하므로, 안내가 나올 때마다 전원을 껐다 켭니다.

메모에는 환자 이름 같은 개인정보를 적지 마세요. 이 저장소는 공개 저장소입니다.

### 저장되는 파일

| 파일 | 내용 |
| --- | --- |
| `session.json` | 포트, 속도, 전송별 길이와 메모 |
| `raw.bin` | 받은 바이트 전체 |
| `timeline.jsonl` | 바이트가 도착한 시각 기록 |
| `burst_001.bin` ... | 전송 한 번씩 나눈 데이터 |
| `scan_9600.bin` ... | 속도 탐색 때 받은 데이터 |

`python -m tsm300.analyze captures/<폴더>` 로 저장된 내용을 다시 볼 수 있습니다.

## 다음 단계

- 캡처를 분석해 결과 해독기 작성, 고막운동도 그래프 이미지 생성
- 백그라운드에서 포트를 지켜보다가 전송이 오면 자동 처리
- 예진이텔레그램(환자 정보), 상쾌한캡처(결과 저장) 연동

장비에 대해 조사한 내용은 [docs/device-notes.md](docs/device-notes.md) 에 있습니다.

## 개발

```bash
python -m pip install -e ".[dev]"
```

```bash
python -m pytest
```
