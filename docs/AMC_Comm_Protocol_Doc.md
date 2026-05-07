# AMC Communication Protocol — Documentation

**Project:** Appcon AMC Motor Controller  
**Document:** Communication Protocol Specification  
**Version:** 1.0  
**Date:** 2026-04-19  
**Author:** Appcon Technologies

---

## 1. Overview

The AMC controller exposes two independent communication protocols over the same physical interface (UART):

| Protocol | Purpose | Master |
|---|---|---|
| **AMCComm** | Motor control commands: SET variables, GET variables, trigger identification routines | GUI (Python application) |
| **DebugComm** | Direct RAM access: read/write any variable by address, configure and retrieve recordings | Octave / MATLAB scripts |

Both protocols share the same UART hardware connection and the same receive buffer in the controller. The **master** is always the **GUI** (Python application) for AMCComm, or the Octave/MATLAB host for DebugComm. The controller is always the **slave** — it never initiates a transmission on its own.

---

## 2. Physical Interface

- **Interface:** UART (serial)
- **Data bits:** 8
- **Stop bits:** 1
- **Parity:** None
- **Baud rate:** Configured at project level (common: 115200 bps)
- **Hardware:** Any STM32-compatible UART peripheral; RX filled via DMA

The physical interface is hardware-independent from the SAL (Software Abstraction Layer) perspective. The SAL communicates only through the shared ring buffer described in Section 3. Changing the physical interface (USB-CDC, CAN, SPI) requires only a HAL modification — the protocol and buffer logic remain unchanged.

---

## 3. The Receive Ring Buffer

### 3.1 Role and Location

The ring buffer is the **sole interface point** between the physical hardware (HAL layer) and the communication protocol parser (SAL layer). It resides in controller RAM and is declared as part of the unified telegram structure:

```c
// SAL_DebugComm.h
typedef struct {
    uint16_t state_u16;
    uint16_t recordConfigFlag_u16;
    uint16_t dataToSendFlag_u16;
    uint16_t dataToSendCount_u16;
    uint16_t calculatedChecksum_u16;
    uint16_t blockSize_u16;

    uint16_t buffer_au16[SAL_DEBUG_TELEGRAM_SIZE]; // <-- The ring buffer
} SAL_DebugTelegram_t;

extern SAL_DebugTelegram_t SAL_DebugTelegram;
```

For AMCComm, only the `buffer_au16[]` field is used. The remaining fields are used exclusively by DebugComm.

The buffer parameters are defined in `SAL_AMCComm_Protocol.h` and `SAL_HAL_BufferInterface.h`:

```c
#define PROTOCOL_INPUT_BUFFER_SIZE  32U  // Must be a power of 2
#define COMM_RX_BUFFER_SIZE         32U  // Must be a power of 2
#define COMM_TX_BUFFER_SIZE         32U  // Must be a power of 2
```

**Size: 32 bytes.** The power-of-2 constraint is mandatory — it enables pointer wrapping using a fast bitwise AND operation instead of a modulo division.

### 3.2 How the Master Writes to the Buffer

The master (GUI) sends characters over the UART. The HAL layer on the controller receives each incoming byte via DMA and places it into successive positions in `buffer_au16[]`. The HAL manages the write pointer autonomously — the SAL parser never writes to the receive buffer.

**Write pointer advancement (HAL side):**

```
write_ptr = (write_ptr + 1) & (PROTOCOL_INPUT_BUFFER_SIZE - 1)
```

The `& (SIZE - 1)` operation wraps the pointer back to 0 when it reaches the end of the buffer, creating the circular (ring) behaviour. Because SIZE is a power of 2, this is a single AND instruction on the MCU.

The HAL fills the buffer continuously regardless of whether the parser has consumed the previous data. There is no flow control or acknowledgement at the buffer level. If the master writes faster than the parser can consume, older bytes are overwritten. In normal operation this does not occur because the parser runs in the main loop at a rate far exceeding the UART byte rate.

### 3.3 How the Parser (SAL) Reads from the Buffer

The parser maintains two independent pointers into the ring buffer:

| Pointer | Name in code | Role |
|---|---|---|
| `msgEnd` | `s_parser.msgEnd` | Scanner — advances byte by byte looking for `;` |
| `msgBegin` | `s_parser.msgBegin` | Reverse scanner — walks back from `msgEnd` to find `#` |
| `localCounter` | `s_parser.localCounter` | Forward scanner — used during word extraction |

**Read pointer advancement (SAL side):**

```
msgEnd = (msgEnd + 1) & (PROTOCOL_INPUT_BUFFER_SIZE - 1)
```

The same bitwise AND wrapping is used. The parser never advances a hardware pointer — it only moves its own software index within the buffer array.

The parser does not copy data out of the buffer. It reads characters in place and marks consumed positions by overwriting them with sentinel values (`@`, `|`, `0`) to prevent re-processing.

### 3.4 Buffer Lifecycle for One Command

```
Step 1 — Master WRITES command into buffer via HAL/DMA:
  Buffer: [... # s   s p e e d   1 0 0 0 ;  ...]
                ^                           ^
                |                           msgEnd advances here on ';'

Step 2 — Parser detects ';' (WAIT_COMPLETE state):
  Buffer: [... # s   s p e e d   1 0 0 0 @  ...]
                                          ^
                                          Overwritten with '@'

Step 3 — Parser walks back to find '#' (FIND_BEGIN state):
  Buffer: [... | s   s p e e d   1 0 0 0 @  ...]
                ^
                '#' replaced with '|'

Step 4 — Parser extracts words into argMatrix (SEPARATE_WORDS):
  argMatrix[0] = "s     "   (command type)
  argMatrix[1] = "speed "   (command name)
  argMatrix[2] = "1000  "   (argument)
  Buffer positions zeroed as they are consumed.

Step 5 — Parser validates and executes (CHECK_ARGS → EXECUTE):
  Calls SAL_ControlMotorSpeed_v(1000)

Step 6 — Parser resets to WAIT_COMPLETE, ready for next command.
```

### 3.5 Buffer Clear Command

The master can send the single character `$` (0x24) at any time to flush the receive buffer:

```c
if (data == PROTOCOL_CLEAR_BUFFER) {   // '$' = 0x24
    memset(inputBuffer, ' ', PROTOCOL_INPUT_BUFFER_SIZE);
}
```

The entire buffer is filled with spaces (0x20). This is used by the GUI's Reset function to recover from corrupted or incomplete messages.

### 3.6 Watchdog / Timeout

A software watchdog protects against the parser becoming stuck on an incomplete or corrupted message:

```c
#define COMM_TIMEOUT_PERIOD  5000000U  // counter ticks
```

The timeout counter increments every call to `SAL_ReadOrder_u16()`. It is reset (`CommKickdog()`) when a valid word separation or execution step begins. If the counter exceeds `COMM_TIMEOUT_PERIOD` without progress, the parser unconditionally returns to `COMM_STATE_WAIT_COMPLETE`, discarding any partial message in progress.

---

## 4. AMCComm Protocol

### 4.1 Message Format

Every command sent by the master follows this structure:

```
#<type> <command> [<argument>];
```

| Field | Character | Meaning |
|---|---|---|
| Begin marker | `#` (0x23) | Mandatory start of every message |
| Type | `s` / `g` | Set or Get |
| Space | ` ` (0x20) | Word separator |
| Command | 6-char name | See command table (Section 4.4) |
| Space | ` ` (0x20) | Word separator (only if argument follows) |
| Argument | Integer string | Numeric value, only for commands that require it |
| End marker | `;` (0x3B) | Mandatory end of every message |
| Newline | `\n` | Appended by the master after `;` |

**Constraints (from firmware):**

```c
#define PROTOCOL_MAX_WORDS    6   // Maximum words per message
#define PROTOCOL_WORD_LENGTH  6   // Maximum characters per word
#define PROTOCOL_MAX_ARGS     6   // Maximum argument count
```

**Examples:**

```
#s stop;             Write — stop motor (no argument)
#s speed 1000;       Write — set speed reference to 1000 RPM
#s contr 3;          Write — set control mode to 3 (Speed)
#g speed;            Read  — get current motor speed
#g err;              Read  — get error word
#s fpwm 16000;       Write — set PWM frequency to 16000 Hz
```

### 4.2 Command Types

| Type char | Enum | Direction | Description |
|---|---|---|---|
| `s` or `S` | `CMD_TYPE_SET` | Write (master → controller) | Write a value or trigger an action |
| `g` or `G` | `CMD_TYPE_GET` | Read (master ← controller) | Request a value from the controller |
| `l` | `CMD_TYPE_LOAD` | Write | Load array data (reserved) |
| `m` | `CMD_TYPE_SAVE` | Write | Save array data (reserved) |

### 4.3 Response Format (GET commands only)

When the master issues a Read (`g`) command, the controller responds with a fixed 12-byte packet:

```
Byte offset:  0    1    2    3    4    5    6    7    8    9   10   11
Content:      '-'  '>'  '\n' '\r' D5   D4   D3   D2   D1   D0  '\n' '\r'
```

| Field | Size | Content |
|---|---|---|
| Header | 4 bytes | `->`  + `\n\r` (LF then CR) |
| Data | 6 bytes | Sign (`+` or `-`) followed by 5 decimal digits |
| Footer | 2 bytes | `\n\r` (LF then CR) |
| **Total** | **12 bytes** | |

**Value encoding:**

All values are signed 16-bit integers encoded as a 6-character ASCII string:

```
+01000   →  positive 1000
-00456   →  negative 456
+00000   →  zero
+32767   →  maximum positive value
-32768   →  minimum negative value (theoretical; int16_t limit)
```

The firmware function `Int16ToAscii6()` performs the encoding. The master (GUI) parses the 6-character string back to a number using standard integer conversion.

**Write commands produce no response.** The master must not wait for a response after a `s` command, except for a possible echoed line from the UART.

### 4.4 Value Scaling

All physical values are transmitted as integers. The master and controller use the following fixed scaling conventions:

| Variable group | Physical unit | Transmitted unit | Scale factor |
|---|---|---|---|
| Currents (isq, isd, dccur, iqmax, idcmx, miqmx) | A | mA | × 1000 |
| Voltages (usd, usq) | V | 0.1 V | × 10 |
| Speed (speed, spmax, msmax, msmnl) | RPM | RPM | × 1 (no scaling) |
| Resistance (mrs) | Ω | mΩ | × 1000 |
| Inductance (mlsd, mlsq) | H | µH | × 1 000 000 |
| Flux linkage (mpsif) | Wb | µWb | × 1 000 000 |
| PWM frequency (fpwm) | Hz | Hz | × 1 (no scaling) |
| Pole pairs (mpole) | — | integer | × 1 (no scaling) |

**Master Write example (current):**  
User sets Isq = 2.5 A → master sends `#s isq 2500;`

**Master Read example (current):**  
Controller responds `+02500` → master displays 2500 / 1000 = 2.5 A

---

## 5. Complete Command Table

### 5.1 Control Commands (no argument)

| Command string | Firmware ID | Direction | Description |
|---|---|---|---|
| `stop  ` | `CMD_STOP_MOTOR` | Write | Stop motor, set control mode OFF |
| `clrerr` | `CMD_CLEAR_ERROR` | Write | Clear active fault / error word |
| `elid  ` | `CMD_ID_ELECTRICAL` | Write | Start electrical identification (Rs, Lsd, Lsq) |
| `psiid ` | `CMD_ID_PSI` | Write | Start PsiF (flux linkage) identification |
| `jid   ` | `CMD_ID_INERTIA` | Write | Start inertia J identification (legacy method) |
| `smpar ` | `CMD_SAVE_MOTOR_PAR` | Write | Save motor parameters to non-volatile memory |
| `suidq ` | `CMD_SETUP_I_CONTROL` | Write | Auto-tune current (Id/Iq) controller |
| `susp  ` | `CMD_SETUP_S_CONTROL` | Write | Auto-tune speed controller |
| `err   ` | `CMD_ERROR` | Read only | Get current error / fault word |

### 5.2 Control Mode and Sensor Mode

| Command string | Firmware ID | Direction | Argument | Values |
|---|---|---|---|---|
| `contr ` | `CMD_CONTROL_MODE` | Read / Write | Mode index (int) | 0=OFF, 1=Position, 2=Speed, 3=Current, 4=Voltage |
| `sens  ` | `CMD_SENSOR_MODE` | Read / Write | Mode index (int) | 0=?, 1=FixAngle, 2=Sensor, 3=Sensorless BEMF, 4=Sensorless HFI |

### 5.3 Motor Control References

| Command string | Firmware ID | Direction | Unit (transmitted) | Physical unit |
|---|---|---|---|---|
| `speed ` | `CMD_MOTOR_SPEED` | Read / Write | RPM | RPM |
| `dccur ` | `CMD_DC_CURRENT` | Read only | mA | A |
| `usd   ` | `CMD_MOTOR_VOLT_D` | Read / Write | 0.1 V | V |
| `usq   ` | `CMD_MOTOR_VOLT_Q` | Read / Write | 0.1 V | V |
| `isd   ` | `CMD_MOTOR_CURR_D` | Read / Write | mA | A |
| `isq   ` | `CMD_MOTOR_CURR_Q` | Read / Write | mA | A |

### 5.4 Limits

| Command string | Firmware ID | Direction | Unit (transmitted) | Physical unit |
|---|---|---|---|---|
| `spmax ` | `CMD_LIMIT_SPEED` | Read / Write | RPM | RPM |
| `iqmax ` | `CMD_LIMIT_ISQ` | Read / Write | mA | A |
| `idcmx ` | `CMD_LIMIT_IDC` | Read / Write | mA | A |
| `accel ` | `CMD_ACCELERATION` | Read / Write | RPM/s | RPM/s |
| `decel ` | `CMD_DECELERATION` | Read / Write | RPM/s | RPM/s |

### 5.5 Motor Parameters

| Command string | Firmware ID | Direction | Unit (transmitted) | Physical unit |
|---|---|---|---|---|
| `mpole ` | `CMD_MOTOR_POLE` | Read / Write | integer | pole pairs |
| `mrs   ` | `CMD_MOTOR_RS` | Read / Write | mΩ | Ω |
| `mlsd  ` | `CMD_MOTOR_LSD` | Read / Write | µH | H |
| `mlsq  ` | `CMD_MOTOR_LSQ` | Read / Write | µH | H |
| `mpsif ` | `CMD_MOTOR_PSIF` | Read / Write | µWb | Wb |
| `miqmx ` | `CMD_MOTOR_ISQ_MAX` | Read / Write | mA | A |
| `msmax ` | `CMD_MOTOR_SPEED_MAX` | Read / Write | RPM | RPM |
| `msmnl ` | `CMD_MOTOR_SPEED_NOLOAD` | Read / Write | RPM | RPM |
| `ioff  ` | `CMD_MOTOR_IS_OFF` | Read / Write | mA | A |
| `itg   ` | `CMD_MOTOR_TG_CURRENT` | Read / Write | (TBD) | current ctrl time constant |
| `itghf ` | `CMD_MOTOR_TG_CURRENT_HF` | Read / Write | (TBD) | HF injection time constant |

### 5.6 Hardware Configuration

| Command string | Firmware ID | Direction | Unit (transmitted) | Physical unit |
|---|---|---|---|---|
| `fpwm  ` | `CMD_SET_FPWM` | Read / Write | Hz | Hz |

---

## 6. Parser State Machine

The parser runs in `SAL_ReadOrder_u16()`, called from `AMCComm_v()` every main-loop cycle.

```
┌─────────────────────────────────────────────────────────────────┐
│                       Parser State Machine                      │
│                                                                 │
│  WAIT_COMPLETE ──► FIND_BEGIN ──► SEPARATE_WORDS               │
│        ▲                                   │                   │
│        │                                   ▼                   │
│        │                           CHECK_ARGS                  │
│        │                                   │                   │
│        │                                   ▼                   │
│        │◄─── (SET) ────────────────── EXECUTE                  │
│        │                                   │                   │
│        │                            (GET) ─▼                   │
│        │◄─── (sent) ──────────────── SEND_DATA                 │
│                                                                 │
│  Any state: watchdog timeout ──► WAIT_COMPLETE (reset)         │
└─────────────────────────────────────────────────────────────────┘
```

| State | Action |
|---|---|
| `WAIT_COMPLETE` | Advances `msgEnd` byte by byte until `;` is found. On `;`: records end position, transitions to `FIND_BEGIN`. On `$`: clears entire buffer. |
| `FIND_BEGIN` | Walks `msgBegin` backwards from `msgEnd` looking for `#`. On `#`: records message boundaries, transitions to `SEPARATE_WORDS`. |
| `SEPARATE_WORDS` | Scans the message byte by byte, splits on spaces, fills `argMatrix[6][6]`. Resets watchdog. Transitions to `CHECK_ARGS`. |
| `CHECK_ARGS` | Validates that `argMatrix[0]` contains valid letters (command type). Parses `argMatrix[2..5]` as signed integers into `argValues[]`. On error: returns to `WAIT_COMPLETE`. |
| `EXECUTE` | Identifies command type (`s`/`g`). Looks up command name in table. Calls SET or GET handler. Resets watchdog. SET → `WAIT_COMPLETE`. GET → `SEND_DATA` if data ready. |
| `SEND_DATA` | Builds 12-byte response packet: `-> \n\r` + 6-char value + `\n\r`. Calls `HAL_COMSendData16Bit_u16()`. On success → `WAIT_COMPLETE`. |

---

## 7. DebugComm Protocol

DebugComm is a separate binary protocol used by Octave/MATLAB for direct RAM access. It uses the same shared `buffer_au16[]` as AMCComm but operates on a completely different telegram structure.

### 7.1 Telegram Structure

The telegram is a fixed 16-word (32-byte) binary frame transmitted as 16-bit words:

```
Word index:  0     1     2     3     4     5     6     7
Content:    Data1 Data2 Data3 Data4 Data5 Data6 Data7 Data8

Word index:  8     9     10    11    12    13    14    15
Content:    Begin Cmd   Type  Count AddrL AddrH CkSum End
```

| Field | Word index | Value / Description |
|---|---|---|
| Data[1..8] | 0–7 | Up to 8 × 16-bit data words (Write) or don't-care (Read) |
| Begin Code | 8 | `0x5555` — mandatory start marker |
| Command | 9 | `0x00C0` = Write, `0x00EE` = Read, `0x023E` = Config Record |
| Data Type | 10 | `0x0001` — standard |
| Count | 11 | Number of 16-bit words to transfer |
| Address Low | 12 | Lower 16 bits of target RAM address |
| Address High | 13 | Upper 16 bits of target RAM address |
| Checksum | 14 | XOR of words 0–13 |
| End Marker | 15 | `0x071B` = normal, `0x00CC` = new data, `0x5757` = sync |

### 7.2 Commands

| Code | Name | Master action |
|---|---|---|
| `0x00C0` | WRITE | Master writes up to 8 words to a specific RAM address |
| `0x00EE` | READ | Master requests up to 8 words from a specific RAM address |
| `0x023E` | CONFIG_REC | Master configures recording: channels, addresses, period, sample count |

### 7.3 Write Transaction

1. Master sends 16-word telegram with Command = `0x00C0`, address, count, and data words
2. Controller validates checksum and address range
3. Controller writes data directly to the specified RAM address
4. Controller sends back a 1-word ACK:

| ACK code | Meaning |
|---|---|
| `0xAC00` | Write executed successfully |
| `0xAC01` | Checksum mismatch — telegram rejected |
| `0xAC02` | Address out of valid RAM range |
| `0xAC03` | Unknown command code |

### 7.4 Read Transaction

1. Master sends 16-word telegram with Command = `0x00EE`, address, and count
2. Controller reads `count` words from the specified RAM address
3. Controller sends back a 16-word telegram with the data in words 0–7

### 7.5 Recording (Config + Retrieve)

1. Master sends CONFIG_REC telegram specifying:
   - Up to 4 channel RAM addresses
   - Number of samples per channel
   - Period divider (sub-sampling relative to ISR rate)
2. Controller records continuously into an internal 4000-word buffer
3. Master retrieves data in blocks using READ telegrams pointing to the recording buffer address

### 7.6 Address Validation

The controller validates all addresses against MCU-specific RAM boundaries:

| MCU | RAM Start | RAM End | Size |
|---|---|---|---|
| STM32F405 / F407 | `0x20000000` | `0x2002FFFF` | 192 KB |
| STM32F427 | `0x20000000` | `0x2003FFFF` | 256 KB |
| STM32G431 | `0x20000000` | `0x20007FFF` | 32 KB |
| STM32G474 | `0x20000000` | `0x2001FFFF` | 128 KB |
| STM32H743 | `0x20000000` | `0x2007FFFF` | 512 KB |
| NXP MKV46F | `0x20000000` | `0x2001FFFF` | 128 KB |

Any address outside these bounds is rejected with ACK `0xAC02`.

### 7.7 MCU Identification

The firmware exposes a global variable `SAL_DebugMcuId_u16` in RAM. At connect time, the Octave `SComm.m` script reads this variable to identify the MCU type and set the correct RAM address bounds for subsequent address validation on the host side. The MCU ID values are:

| Value | MCU |
|---|---|
| `0x0001` | NXP MKV46F |
| `0x0002` | STM32F405 |
| `0x0003` | STM32F407 |
| `0x0004` | STM32F427 |
| `0x0005` | STM32G431 |
| `0x0006` | STM32G474 |
| `0x0007` | STM32H743 |

---

## 8. Shared Buffer Architecture

Both protocols share the same physical UART and the same receive buffer:

```
┌──────────────┐     UART / DMA      ┌────────────────────────────────────┐
│              │ ──── bytes ────────► │  buffer_au16[32]  (ring buffer)    │
│    Master    │                      │  in SAL_DebugTelegram.buffer_au16  │
│  (GUI /      │                      ├────────────────────────────────────┤
│   Octave)    │ ◄─── response ─────  │  TX path: HAL_COMSendData16Bit()  │
└──────────────┘                      └────────────────────────────────────┘
                                                     │
                              ┌──────────────────────┴───────────────────┐
                              │                                           │
                              ▼                                           ▼
                     ┌────────────────┐                        ┌─────────────────┐
                     │   AMCComm      │                        │   DebugComm     │
                     │  SAL_ReadOrder │                        │  SAL_DebugProc  │
                     │  (text parser) │                        │  (binary parser)│
                     └────────────────┘                        └─────────────────┘
                              │                                           │
                              ▼                                           ▼
                     Motor control calls                    Direct RAM read/write
                     (speed, current,                      (any variable, recording
                      voltage, params)                      configuration)
```

The two parsers are mutually exclusive in practice: AMCComm is called from the main loop when the GUI is active; DebugComm is called when Octave is connected. Both read from the same buffer but use completely different framing, so they do not interfere as long as only one master is active at a time.

---

## 9. Error Handling

### 9.1 Error Word (AMCComm)

The error word is a 6-character string read via `#g err;`. Each character position encodes a specific fault:

| Character | Fault |
|---|---|
| `i` | Software over-current |
| `v` | Software over-voltage |
| `I` | Hardware fault (HW_FAULT) |
| `b` | Break error (ERROR_BREAK) |
| `o` | Current offset error |
| `t` | MOS over-temperature |

A response of `+00000` (all zeros) indicates no fault. The master reads this string and maps each character to its fault description for display.

### 9.2 Parser Error Recovery

The parser recovers automatically from any of the following:
- Incomplete message (no `;` received within timeout period)
- Invalid characters in command type field
- Non-numeric characters in argument field
- Unknown command name (not in command table)

In all cases the parser resets silently to `WAIT_COMPLETE`. No error response is sent to the master for AMCComm parse errors.

### 9.3 DebugComm Error Responses

DebugComm returns explicit ACK codes (see Section 7.3) for every Write telegram. Read errors result in no data being sent back (master timeout).

---

## 10. Revision History

| Version | Date | Description |
|---|---|---|
| 1.0 | 2026-04-19 | Initial documentation — derived from firmware source `SAL_AMCComm.c` v2026-01-27, `SAL_DebugComm.h` v2.1, `SAL_AMCComm_CmdTable.c` v2026-01-26 |