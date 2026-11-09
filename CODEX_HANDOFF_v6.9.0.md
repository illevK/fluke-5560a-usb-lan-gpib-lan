# Codex 이어하기 작업 인계 문서 (v6.9.0)

- **저장소**: `https://github.com/illevK/fluke-5560a-usb-lan-gpib-lan`
- **로컬 루트 경로**: `C:\Users\HCT-2714\Documents\Codex\2026-10-29\fluke-5560a-usb-lan-gpib-lan`
- **현재 버전**: `v6.9.0` (`outputs/fluke_5560a_controller.py`, `outputs/Fluke 교정기.exe`, 바탕화면 `Fluke 교정기.exe`)

## 1. v6.9.0 패치 핵심 요약

1. **WT310E 시작 시 `WT 데이터 갱신 이벤트 설정을 확인할 수 없습니다.` 오류 해결**
   - `_read_wt_numbers()`에서 `:STATUS:FILTER1?` 응답이 축약형 `"NEV"`(또는 헤더 포함 문자열)로 올 때 `"NEVER"`로 정규화하고, 미인식 시에도 `:STATUS:FILTER1 FALL`을 직접 설정 후 확인.
2. **DUT 연결(`DUTSession.connect`) 직후 전 장비 공통 디폴트 설정(`_configure_dut_defaults`) 자동 적용**
   - **Update Rate (`Urate`)**: WT 전 계열 `:RATE 250MS`, PPA 계열 `SPEED,FAST`
   - **Mode (`RMS`)**: WT310/332/333 `:INPUT:MODE RMS`, WT3000 계열 `:INPUT:VOLTAGE:MODE RMS` & `:INPUT:CURRENT:MODE RMS`, PPA `COUPLI,AC+DC` & `RESOLU,HIGH`
   - **Sync (`전압`)**: 1채널 WT310 계열 `:INPUT:SYNCHRONIZE VOLTAGE`, 다채널 WT 계열 `:INPUT:SYNCHRONIZE:ELEMENT<ch> U<ch>`, PPA `SYNC,VOLT`
   - **Range (`V/I 모두 AUTO`)**: WT 전 계열 `:INPUT:VOLTAGE:AUTO ON` & `:INPUT:CURRENT:AUTO ON`, PPA `AUTORANG,ON`
   - **Hold 해제 및 필터**: `:HOLD OFF`, `:MEASURE:MHOLD OFF`, `:NUMERIC:HOLD OFF`, `:STATUS:FILTER1 FALL`
3. **Fluke 5560A LAN 유선 랜카드(`17: 이더넷`) 우선 바인딩 및 보조 IP 복구**
   - `AutoNetworkAligner.select_source_ip()`에서 `자동` 선택 시에도 `Up` 상태인 유선 랜카드를 우선 반환.
   - `17: 이더넷`에 `169.254.1.100/16` 및 `192.168.100.100/24` 보조 IP 자동 유지.
4. **바탕화면 EXE 배포 주의사항**
   - `work/package_exe.py`는 내부적으로 `work/build_improved.py`를 호출해 `outputs/fluke_5560a_controller.py`를 덮어쓰므로, 수정 사항은 반드시 `work/dut_support.pyfrag` 및 `work/build_improved.py`에 반영하고 빌드 후 바탕화면(`Desktop\Fluke 교정기.exe`)까지 복사해야 함.
