/** Every server request passes through here, so one place can see a session end. */
type StatusObserver = (status: number) => void;

let observer: StatusObserver = () => {};

export function observeStatuses(next: StatusObserver) {
  observer = next;
}

/** For a transport that cannot use apiFetch, such as an upload that reports its progress. */
export function reportStatus(status: number) {
  observer(status);
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit) {
  // eslint-disable-next-line no-restricted-globals -- the one place allowed to call it
  const response = await fetch(input, init);
  observer(response.status);
  return response;
}
