// Legacy test: binary counter with Gray-coded output.
module gray_counter #(parameter W = 4) (
  input              clk, rst,
  output reg [W-1:0] g
);
  reg [W-1:0] b;
  always @(posedge clk)
    if (rst) begin b <= {W{1'b0}}; g <= {W{1'b0}}; end
    else begin b <= b + 1'b1; g <= (b + 1'b1) ^ ((b + 1'b1) >> 1); end
endmodule
