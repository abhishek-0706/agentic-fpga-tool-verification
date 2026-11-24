// Legacy test: 32-bit parity and all-ones/all-zeros detection.
// Note: expectation block in the manifest was copied from the multiplier test.
module parity32 (
  input  [31:0] x,
  output        par,
  output        ones,
  output        zeros
);
  assign par   = ^x;
  assign ones  = &x;
  assign zeros = ~|x;
endmodule
