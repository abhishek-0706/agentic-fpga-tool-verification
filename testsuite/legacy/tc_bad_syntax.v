// Legacy test: 4-bit comparator with equality and greater-than outputs.
module cmp4 (
  input  [3:0] a, b,
  output       eq,
  output       gt
);
  assign eq = (a == b)
  assign gt = (a > b);
endmodule
