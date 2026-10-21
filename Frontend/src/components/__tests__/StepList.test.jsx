import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import StepList, { addStep } from '../StepList';

describe('StepList', () => {
  it('shows finished steps and the current one while working', () => {
    render(<StepList steps={['Looking up Arrival', 'Checking where it streams']} working />);
    const items = screen.getAllByRole('listitem');
    expect(items[0]).toHaveClass('is-done');
    expect(items[1]).toHaveAttribute('aria-current', 'step');
  });

  it('folds into a disclosure once the answer is written, and hides a lone step', () => {
    const { rerender, container } = render(<StepList steps={['Looking up Arrival', 'Checking where it streams']} working={false} />);
    expect(screen.getByText('2 steps').tagName).toBe('SUMMARY');
    rerender(<StepList steps={['Thinking']} working={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('records each new step once', () => {
    expect(addStep(undefined, 'Thinking')).toEqual(['Thinking']);
    expect(addStep(['Thinking'], 'Thinking')).toEqual(['Thinking']);
    expect(addStep(['Thinking'], '  ')).toEqual(['Thinking']);
    expect(addStep(['A'], 'B')).toEqual(['A', 'B']);
  });
});
