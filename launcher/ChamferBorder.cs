using System;
using System.Windows;
using System.Windows.Media;

namespace AbyssLauncher;

/// <summary>斜切角位置。可组合（如左上 + 右下 = Diagonal）。</summary>
[Flags]
public enum ChamferCorners
{
    None = 0,
    TopLeft = 1,
    TopRight = 2,
    BottomLeft = 4,
    BottomRight = 8,
    Diagonal = TopLeft | BottomRight,
    All = TopLeft | TopRight | BottomLeft | BottomRight,
}

/// <summary>
/// 斜切角容器：CMF 形态语言里「斜切」的 WPF 实现。
///
/// WPF 的 Border 只能做圆角矩形（CornerRadius），做不出切角，
/// 所以这里自己拼几何。内容布局仍交给 Decorator（子元素填满），
/// 斜切只影响背景与描边。
///
/// 用法：
///   &lt;local:ChamferBorder Chamfer="8" Corners="Diagonal"
///                         Background="{DynamicResource BrushBgElevated}"
///                         BorderBrush="{DynamicResource BrushBorderDefault}"
///                         BorderThickness="1"&gt;…&lt;/local:ChamferBorder&gt;
/// </summary>
public class ChamferBorder : System.Windows.Controls.Decorator
{
    public static readonly DependencyProperty ChamferProperty =
        DependencyProperty.Register(nameof(Chamfer), typeof(double), typeof(ChamferBorder),
            new FrameworkPropertyMetadata(8d, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty CornersProperty =
        DependencyProperty.Register(nameof(Corners), typeof(ChamferCorners), typeof(ChamferBorder),
            new FrameworkPropertyMetadata(ChamferCorners.Diagonal, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty BackgroundProperty =
        DependencyProperty.Register(nameof(Background), typeof(Brush), typeof(ChamferBorder),
            new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty BorderBrushProperty =
        DependencyProperty.Register(nameof(BorderBrush), typeof(Brush), typeof(ChamferBorder),
            new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty BorderThicknessProperty =
        DependencyProperty.Register(nameof(BorderThickness), typeof(Thickness), typeof(ChamferBorder),
            new FrameworkPropertyMetadata(new Thickness(0), FrameworkPropertyMetadataOptions.AffectsRender));

    public double Chamfer
    {
        get => (double)GetValue(ChamferProperty);
        set => SetValue(ChamferProperty, value);
    }

    public ChamferCorners Corners
    {
        get => (ChamferCorners)GetValue(CornersProperty);
        set => SetValue(CornersProperty, value);
    }

    public Brush? Background
    {
        get => (Brush?)GetValue(BackgroundProperty);
        set => SetValue(BackgroundProperty, value);
    }

    public Brush? BorderBrush
    {
        get => (Brush?)GetValue(BorderBrushProperty);
        set => SetValue(BorderBrushProperty, value);
    }

    public Thickness BorderThickness
    {
        get => (Thickness)GetValue(BorderThicknessProperty);
        set => SetValue(BorderThicknessProperty, value);
    }

    // Decorator 基类并没有 Padding（只有 Border 有），这里自己补一个
    public static readonly DependencyProperty PaddingProperty =
        DependencyProperty.Register(nameof(Padding), typeof(Thickness), typeof(ChamferBorder),
            new FrameworkPropertyMetadata(new Thickness(0),
                FrameworkPropertyMetadataOptions.AffectsMeasure | FrameworkPropertyMetadataOptions.AffectsArrange));

    public Thickness Padding
    {
        get => (Thickness)GetValue(PaddingProperty);
        set => SetValue(PaddingProperty, value);
    }

    // 内容按 Padding 内缩排布。注意不能用 Decorator 基类的实现：
    // 它不会把 Padding 算进布局（只有 Border 会），那样样式里的 Padding 会被无声忽略。
    protected override Size MeasureOverride(Size constraint)
    {
        var child = Child;
        if (child is null) return new Size();

        var inner = new Size(
            Math.Max(0, constraint.Width - Padding.Left - Padding.Right),
            Math.Max(0, constraint.Height - Padding.Top - Padding.Bottom));
        child.Measure(inner);

        return new Size(
            child.DesiredSize.Width + Padding.Left + Padding.Right,
            child.DesiredSize.Height + Padding.Top + Padding.Bottom);
    }

    protected override Size ArrangeOverride(Size arrangeSize)
    {
        Child?.Arrange(new Rect(
            Padding.Left,
            Padding.Top,
            Math.Max(0, arrangeSize.Width - Padding.Left - Padding.Right),
            Math.Max(0, arrangeSize.Height - Padding.Top - Padding.Bottom)));
        return arrangeSize;
    }

    protected override void OnRender(DrawingContext dc)
    {
        double w = ActualWidth, h = ActualHeight;
        if (w <= 0 || h <= 0) return;

        // 切角长度不能超过短边的一半，否则几何自交
        double c = Math.Max(0, Math.Min(Chamfer, Math.Min(w, h) / 2));
        var b = BorderThickness;

        // 描边是居中描：把几何往里收半个线宽，免得一半被裁掉
        double x0 = b.Left / 2, y0 = b.Top / 2;
        double x1 = w - b.Right / 2, y1 = h - b.Bottom / 2;
        if (x1 <= x0 || y1 <= y0) return;

        bool tl = Corners.HasFlag(ChamferCorners.TopLeft);
        bool tr = Corners.HasFlag(ChamferCorners.TopRight);
        bool bl = Corners.HasFlag(ChamferCorners.BottomLeft);
        bool br = Corners.HasFlag(ChamferCorners.BottomRight);

        var geometry = new StreamGeometry();
        using (var ctx = geometry.Open())
        {
            ctx.BeginFigure(new Point(x0 + (tl ? c : 0), y0), Background is not null, true);
            ctx.LineTo(new Point(x1 - (tr ? c : 0), y0), true, false);
            if (tr) ctx.LineTo(new Point(x1, y0 + c), true, false);
            ctx.LineTo(new Point(x1, y1 - (br ? c : 0)), true, false);
            if (br) ctx.LineTo(new Point(x1 - c, y1), true, false);
            ctx.LineTo(new Point(x0 + (bl ? c : 0), y1), true, false);
            if (bl) ctx.LineTo(new Point(x0, y1 - c), true, false);
            ctx.LineTo(new Point(x0, y0 + (tl ? c : 0)), true, false);
        }
        geometry.Freeze();

        if (Background is not null) dc.DrawGeometry(Background, null, geometry);

        double thickness = Math.Max(b.Left, Math.Max(b.Top, Math.Max(b.Right, b.Bottom)));
        if (BorderBrush is not null && thickness > 0)
        {
            var pen = new Pen(BorderBrush, thickness);
            pen.Freeze();
            dc.DrawGeometry(null, pen, geometry);
        }
    }
}
