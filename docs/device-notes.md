# Oscilla TSM-300 조사 메모

제조사: Inmedico A/S (덴마크), 상표 Oscilla. 스크리닝용 임피던스 청력검사기.

## 확인된 사실

출처는 TSM-300 사용 설명서(Instructions for Use, 21쪽)와 제품 브로슈어입니다.

### 연결

- 뒷면 `SERIAL` 단자는 RS-232이며 PC 전송 또는 시리얼 프린터 출력에 쓴다. `PRINTER` 단자는 병렬 프린터용이다.
- PC와는 널모뎀 케이블로 연결한다. PC에 시리얼 포트가 없으면 USB-시리얼 변환기를 쓸 수 있다.
- 전용 PC 프로그램은 AudioConsole(별매)이다.

### 전송 방식

- 오른쪽 화살표 키가 출력 키다. `MENU` -> `Right key` 에서 `PRINT` / `TRANSMIT` 을 고른다.
- `TRANSMIT` 상태에서 오른쪽 키를 누르면 화면의 검사가 PC로 전송된다. 검사 후 자동 전송된다는 설명은 없다.
- 전원을 켤 때 장비가 시리얼 포트로 식별 키(identification key)를 자동으로 보낸다. PC 프로그램이 tympanometer 모드로 바뀌는 데 쓴다.
- `Printer Port` 가 `Serial` 이면 식별 키를 보내지 않는다.

### 저장과 결과 항목

- 메모리에 20개 측정을 저장한다. ID 위치마다 양쪽 귀의 그래프와 수치를 모두 저장한다.
- 화면 수치: `MID EAR PRESS`(daPa), `EAR VOLUME`(ml), `COMPLIANCE`(ml), `TYMP WIDTH`(daPa).
- 반사 검사: 동측, 500 / 1000 / 2000 / 3000 / 4000 Hz, 105 dB SPL. 컴플라이언스 변화가 0.05 ml 를 넘으면 반사 있음.

### 사양

- 프로브 톤 226 Hz, 85 dB SPL
- 압력 범위 +200 ~ -300 daPa
- 용적 범위 0.1 ~ 6.0 ml
- 교정 캐비티 0.5 / 2.0 / 5.0 ml

### 프린터 출력 (대안 경로)

- 프린터 종류: `MATRIX`(Epson 호환), `IBM PRO`, `THERMO`(시리얼 감열 프린터), `HP`(PCL).
- `Printer Port` 를 `Serial` 로 바꾸면 시리얼 포트로 인쇄 데이터가 나온다. `TRANSMIT` 해독이 막히면 이 인쇄 데이터를 받아 이미지로 바꾸는 방법을 쓸 수 있다.

## 확인되지 않은 것

- 통신 속도와 프레임 설정. 설명서에 없다. 같은 제조사의 SM930 청력검사기는 9600 bps 8N1 이라 TSM-300도 같을 가능성이 있다.
- 커넥터 종류와 핀 배열.
- 식별 키의 내용.
- 전송 데이터의 형식. PC가 먼저 응답해야 전송이 이어지는지도 모른다.

## 주의

- `MENU` -> `Calib cavity` 는 교정값을 바꾸는 메뉴다. 캡처 시험에 캐비티를 쓸 때는 이 메뉴에 들어가지 말고 일반 측정 화면에서 프로브만 캐비티에 꽂는다.
- `MENU` -> `Clear data` 는 저장된 측정을 지운다.

## 출처

- [TSM-300 Instructions for Use (ManualsLib)](https://www.manualslib.com/manual/1273759/Oscilla-Tsm-300.html)
- [TSM-300 브로슈어](http://audiometry.co.za/pdf/oscillatsm300_uk.pdf)
- [SM930 사용 설명서](https://manualzz.com/doc/6779952/oscilla-sm930-audiometer-user-manual)
