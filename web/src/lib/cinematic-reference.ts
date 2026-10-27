/** Match the saved-revision measurement service's cardiac name families. */
export function hasCinematicReferenceMasks(structures: readonly { name: string }[]): boolean {
  const names = structures.map(item => item.name.toLowerCase().replace(/[_-]/g, ' ').replace(/\s+/g, ' ').trim());
  return (
    names.some(name => name.includes('myocard')) &&
    names.some(name => name.includes('aort') || ['left ventric', 'ventricle left', 'left atri', 'atrium left'].some(word => name.includes(word)))
  );
}

/** A failed or cancelled automatic request cannot create a reactive retry loop. */
export class CinematicReferenceGate {
  private key = '';
  private attempted = false;
  private manual = false;

  select(key: string) {
    if (key === this.key) return;
    this.key = key;
    this.attempted = false;
    this.manual = false;
  }
  edited(key: string) {
    this.select(key);
    this.manual = true;
  }
  claim(key: string): boolean {
    this.select(key);
    if (!key || this.attempted || this.manual) return false;
    this.attempted = true;
    return true;
  }
}
