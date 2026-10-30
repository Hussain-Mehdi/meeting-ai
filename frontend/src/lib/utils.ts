import {clsx, type ClassValue} from 'clsx';
import {twMerge} from 'tailwind-merge';

/** Class name helper used by the components adapted from cult-ui. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
