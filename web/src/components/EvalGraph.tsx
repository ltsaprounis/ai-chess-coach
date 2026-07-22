import type { MoveEval } from "../api.ts";

const CLAMP = 600; // centipawns shown at full scale
const HEIGHT = 120;
const PLY_WIDTH = 8;

function displayValue(moveEval: MoveEval): number {
  if (moveEval.eval_mate !== null) {
    return moveEval.eval_mate > 0 ? CLAMP : -CLAMP;
  }
  const cp = moveEval.eval_cp ?? 0;
  return Math.max(-CLAMP, Math.min(CLAMP, cp));
}

type Props = {
  evals: MoveEval[];
  selectedPly: number; // 0 = start position
  onSelect: (ply: number) => void;
};

/** White-POV eval per ply; white advantage plots above the midline. */
export default function EvalGraph({ evals, selectedPly, onSelect }: Props) {
  const width = Math.max(240, evals.length * PLY_WIDTH);
  const mid = HEIGHT / 2;
  const x = (ply: number) => (ply / Math.max(1, evals.length)) * width;
  const y = (value: number) => mid - (value / CLAMP) * (mid - 6);

  const points = [`0,${mid}`];
  for (const moveEval of evals) {
    points.push(`${x(moveEval.ply)},${y(displayValue(moveEval))}`);
  }
  const area = `0,${mid} ${points.join(" ")} ${width},${mid}`;

  const pick = (event: React.MouseEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const fraction = (event.clientX - rect.left) / rect.width;
    const ply = Math.round(fraction * evals.length);
    onSelect(Math.max(0, Math.min(evals.length, ply)));
  };

  const step = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      onSelect(Math.max(0, selectedPly - 1));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      onSelect(Math.min(evals.length, selectedPly + 1));
    }
  };

  return (
    <button
      type="button"
      className="eval-graph"
      aria-label="Evaluation by move — click to jump, arrow keys to step"
      onClick={pick}
      onKeyDown={step}
    >
      <svg
        viewBox={`0 0 ${width} ${HEIGHT}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        <rect x={0} y={0} width={width} height={HEIGHT} className="eg-bg" />
        <polygon points={area} className="eg-area" />
        <line x1={0} y1={mid} x2={width} y2={mid} className="eg-mid" />
        <polyline points={points.join(" ")} fill="none" className="eg-line" />
        <line
          x1={x(selectedPly)}
          y1={0}
          x2={x(selectedPly)}
          y2={HEIGHT}
          className="eg-cursor"
        />
      </svg>
    </button>
  );
}
