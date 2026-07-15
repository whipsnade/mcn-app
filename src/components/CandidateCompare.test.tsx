import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { candidatePage } from '../test/fixtures';
import CandidateCompare from './CandidateCompare';

describe('CandidateCompare', () => {
  it('shows selected candidates and the six score dimensions', () => {
    render(<CandidateCompare candidates={candidatePage.items} />);

    expect(screen.getByText('达人甲')).toBeVisible();
    expect(screen.getByText('受众匹配')).toBeVisible();
    expect(screen.getByText('风险控制')).toBeVisible();
  });
});
