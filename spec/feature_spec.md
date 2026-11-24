# Feature specification: FPGA synthesis (open-source analogue)

Tool under test: Yosys synthesis (`synth` and `synth_xilinx`, AMD/Xilinx 7-series target),
run through a wrapper that emulates two builds of the tool: `buggy` (with injected defects)
and `fixed`. This is an open-source stand-in for a commercial FPGA synthesis tool; it is not
Vivado.

The verification agent reads this file to build its test plan. Each feature lists the Verilog
constructs it covers, the generators that produce testcases for it, resource expectations
(QoR sanity) per target family, the flows it must be exercised on, and a priority.

Expectation syntax: `<FAMILY or CELL> >= <n>`. Families: DSP, BRAM, LUTRAM, SRL, CARRY, LUT,
FF, MUXF. Family `xc7` expectations apply to every `synth_xilinx` flow; `generic` to `synth`.

## Flows

| id | command | regression |
|----|---------|------------|
| generic | `synth` | yes |
| xc7 | `synth_xilinx -family xc7` | yes |
| xc7_flatten | `synth_xilinx -family xc7 -flatten` | yes |
| xc7_noiopad | `synth_xilinx -family xc7 -noiopad` | planned |
| xc7_dff | `synth_xilinx -family xc7 -dff` | yes |
| xc7_flat_noio | `synth_xilinx -family xc7 -flatten -noiopad` | yes |
| xc7_widemux | `synth_xilinx -family xc7 -widemux 5` | planned |

## Features

### FEAT-COMB: Combinational logic, case/casez priority logic, reduction operators
- constructs: continuous assign, always @*, case, casez, reduction operators
- generators: comb_mux
- expect.xc7: LUT>=1
- flows: all
- priority: P2

### FEAT-ARITH: Adders and subtractors mapped to carry chains
- constructs: + and - operators, carry out
- generators: addsub
- expect.xc7: CARRY>=1
- flows: all
- priority: P1

### FEAT-DSP: Multiplier and multiply-accumulate inference into DSP48E1
- constructs: * operator, registered product, accumulator feedback
- generators: mul_reg, macc
- expect.xc7: DSP>=1
- flows: all
- priority: P1

### FEAT-FSM: Finite state machine extraction and re-encoding
- constructs: state register, case-based next-state logic, registered Mealy output
- generators: fsm_seqdet
- flows: all
- priority: P1

### FEAT-SEQ: Sequential logic with synchronous reset and clock enable
- constructs: always @(posedge clk), synchronous reset, enable, up/down counting
- generators: counter, fsm_seqdet, macc
- flows: all
- priority: P1

### FEAT-SRL: Shift registers mapped to SRL16E/SRLC32E
- constructs: shift by concatenation with enable, no reset
- generators: shreg
- expect.xc7: SRL>=1
- flows: all
- priority: P2

### FEAT-LUTRAM: Small memories with asynchronous read mapped to distributed RAM
- constructs: reg array, synchronous write, asynchronous read
- generators: lutram
- expect.xc7: LUTRAM>=1
- flows: all
- priority: P1

### FEAT-BRAM: Memories with synchronous read mapped to block RAM
- constructs: reg array, ram_style attribute, read-first synchronous read
- generators: bram
- expect.xc7: BRAM>=1
- flows: all
- priority: P1

### FEAT-PARAM: Parameterised modules and parameter overrides
- constructs: parameter, #( ) override, width expressions
- generators: counter, param_hier
- flows: all
- priority: P2

### FEAT-HIER: Hierarchy, generate loops and flattening
- constructs: submodule instances, generate for, indexed part-select
- generators: param_hier
- flows: all
- priority: P1

### FEAT-ASYNC-RST: Asynchronous reset and preset flip-flops (FDCE/FDPE)
- constructs: always @(posedge clk or posedge arst)
- generators:
- flows: all
- priority: P2

### FEAT-WIDEMUX: Wide multiplexers mapped to MUXF7/MUXF8
- constructs: 16:1 and 32:1 multiplexers, -widemux option
- generators:
- flows: xc7, xc7_widemux
- priority: P3
