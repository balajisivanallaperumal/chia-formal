// Round-robin arbiter with a rotating priority pointer.
// Harder than the FIFO on purpose: the interesting invariants are one-hot
// grant, grant-implies-request, and forward progress of the priority pointer --
// none of which a few directed request patterns exercise.
module arbiter #(
    parameter N  = 4,
    parameter PW = 2               // $clog2(N)
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire [N-1:0]  req,
    output reg  [N-1:0]  grant,
    output wire          any_grant
);

    reg [PW-1:0] prio;             // index that gets first refusal this cycle

    // rotate requests so the priority index sits at bit 0, pick lowest set bit,
    // then rotate the winner back into place
    wire [N-1:0] rot_req  = (req >> prio) | (req << (N - prio));
    wire [N-1:0] rot_pick = rot_req & (~rot_req + 1'b1);
    wire [N-1:0] pick     = (rot_pick << prio) | (rot_pick >> (N - prio));

    assign any_grant = |grant;

    always @(posedge clk) begin
        if (!rst_n) grant <= {N{1'b0}};
        else        grant <= pick;
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            prio <= {PW{1'b0}};
        end else if (|pick) begin
            // next cycle, priority moves just past the winner
            case (pick)
                4'b0001: prio <= 2'd1;
                4'b0010: prio <= 2'd2;
                4'b0100: prio <= 2'd3;
                4'b1000: prio <= 2'd0;
                default: prio <= prio;
            endcase
        end
    end

// PROPERTIES_INJECTION_POINT
endmodule
