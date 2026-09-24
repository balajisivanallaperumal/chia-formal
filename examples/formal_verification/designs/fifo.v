// Synchronous FIFO with extra-bit pointer wrap detection.
// The DUT under study: small enough for BMC to converge, but with the
// genuine corner cases (wrap, simultaneous rd+wr, full/empty boundaries)
// that directed tests routinely under-cover.
module fifo #(
    parameter WIDTH = 8,
    parameter DEPTH = 8,           // must be a power of two
    parameter AW    = 3            // $clog2(DEPTH)
) (
    input  wire             clk,
    input  wire             rst_n,

    input  wire             wr_en,
    input  wire [WIDTH-1:0] wr_data,

    input  wire             rd_en,
    output wire [WIDTH-1:0] rd_data,

    output wire             full,
    output wire             empty,
    output wire [AW:0]      count
);

    reg [WIDTH-1:0] mem [0:DEPTH-1];
    reg [AW:0]      wptr;          // one extra bit distinguishes full from empty
    reg [AW:0]      rptr;

    wire do_wr = wr_en && !full;
    wire do_rd = rd_en && !empty;

    always @(posedge clk) begin
        if (!rst_n) begin
            wptr <= {(AW+1){1'b0}};
        end else if (do_wr) begin
            mem[wptr[AW-1:0]] <= wr_data;
            wptr <= wptr + 1'b1;
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            rptr <= {(AW+1){1'b0}};
        end else if (do_rd) begin
            rptr <= rptr + 1'b1;
        end
    end

    assign rd_data = mem[rptr[AW-1:0]];
    assign empty   = (wptr == rptr);
    assign full    = (wptr[AW] != rptr[AW]) && (wptr[AW-1:0] == rptr[AW-1:0]);
    assign count   = wptr - rptr;

// PROPERTIES_INJECTION_POINT
endmodule
