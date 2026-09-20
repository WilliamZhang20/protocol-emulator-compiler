#include "Proto/ProtoOps.h"
#include "Action/ActionOps.h"

#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/DialectConversion.h"

namespace transforms {

struct DriveLowering : public mlir::OpRewritePattern<proto::DriveOp> {
    using OpRewritePattern::OpRewritePattern;

    mlir::LogicalResult matchAndRewrite(proto::DriveOp op, mlir::PaternRewriter &rewriter) const override {
        rewriter.ReplaceOpWithNewOp<Action::Gpio::Op>(
	    op,
	    op.getPin(),
	    op.getValue());
	
	return mlir::Success(); 
    }
};


struct DelayLowering : public mlir::OpRewritePattern<proto::DelayOp> {

  using OpRewritePattern::OpRewritePattern;

  mlir::LogicalResult matchAndRewrite(proto::DelayOp op,
      mlir::PatternRewriter &rewriter) const override {
          rewriter.replaceOpWithNewOp<action::DelayOp>(
             op,
             op.getCycles());

          return mlir::success();
      }
};

struct SampleLowering : public mlir::OpRewritePattern<proto::SampleOp> {
  using OpRewritePattern::OpRewritePattern;

  mlir::LogicalResult matchAndRewrite(proto::SampleOp op,
                  mlir::PatternRewriter &rewriter) const override {
    auto newOp =
        rewriter.create<action::SampleOp>(
            op.getLoc(),
            rewriter.getI1Type(),
            op.getPin());

    rewriter.replaceOp(op, newOp.getValue());
    return mlir::success();
  }
};

struct LowerProtoToActionPass
    : public mlir::PassWrapper<
          LowerProtoToActionPass,
          mlir::OperationPass<mlir::ModuleOp>> {

  void runOnOperation() override {
    mlir::MLIRContext *ctx = &getContext();

    mlir::RewritePatternSet patterns(ctx);

    patterns.add<
        DriveLowering,
        DelayLowering,
        SampleLowering
    >(ctx);

    mlir::ConversionTarget target(*ctx);

    target.addLegalDialect<action::ActionDialect>();
    target.addLegalDialect<mlir::BuiltinDialect>();

    target.addIllegalDialect<proto::ProtoDialect>();

    if (mlir::failed(
            mlir::applyPartialConversion(
                getOperation(),
                target,
                std::move(patterns)))) {
      signalPassFailure();
    }
  }
};

}
