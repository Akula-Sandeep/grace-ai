import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ProjectsSection from '../ProjectsSection';

const dateNight = { id: 3, name: 'Date night', instructions: 'Under two hours.', conversation_count: 2 };

describe('ProjectsSection', () => {
  it('creates a project with instructions and opens it', async () => {
    const onCreate = vi.fn().mockResolvedValue({ id: 9, name: 'Horror marathon' });
    const onOpen = vi.fn();
    render(<ProjectsSection projects={[]} activeProject={null} onOpen={onOpen} onCreate={onCreate} onUpdate={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText(/Group chats with shared instructions/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'New project' }));
    const dialog = screen.getByRole('dialog', { name: 'New project' });
    expect(screen.getByLabelText('Name')).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Create project' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: ' Horror marathon ' } });
    fireEvent.change(screen.getByLabelText(/Instructions for every chat/), { target: { value: 'Only horror. ' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    });
    expect(onCreate).toHaveBeenCalledWith({ name: 'Horror marathon', instructions: 'Only horror.' });
    expect(onOpen).toHaveBeenCalledWith(9);
    expect(dialog).not.toBeInTheDocument();
  });

  it('opens and closes a project, and removing one asks first', async () => {
    const onOpen = vi.fn();
    const onDelete = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ProjectsSection projects={[dateNight]} activeProject={null} onOpen={onOpen} onCreate={vi.fn()} onUpdate={vi.fn()} onDelete={onDelete} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /^Date night/ }));
    expect(onOpen).toHaveBeenLastCalledWith(3);
    rerender(<ProjectsSection projects={[dateNight]} activeProject={dateNight} onOpen={onOpen} onCreate={vi.fn()} onUpdate={vi.fn()} onDelete={onDelete} />);
    fireEvent.click(screen.getByRole('button', { name: /^Date night/ }));
    expect(onOpen).toHaveBeenLastCalledWith(null);

    fireEvent.click(screen.getByRole('button', { name: 'Edit project Date night' }));
    expect(screen.getByLabelText('Name')).toHaveValue('Date night');
    fireEvent.click(screen.getByRole('button', { name: 'Remove…' }));
    expect(onDelete).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove project (chats stay)' }));
    });
    expect(onDelete).toHaveBeenCalledTimes(1);
  });
});
