interface DisclaimerProps {
  text: string;
}

/** The medical disclaimer is always rendered, in every state. */
export function Disclaimer({ text }: DisclaimerProps) {
  return (
    <div className="note" role="note" data-testid="disclaimer">
      <span aria-hidden="true">{"⚑"} </span>
      {text}
    </div>
  );
}
