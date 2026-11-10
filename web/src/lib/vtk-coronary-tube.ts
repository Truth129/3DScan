import '@kitware/vtk.js/Rendering/OpenGL/Actor';
import '@kitware/vtk.js/Rendering/OpenGL/PolyDataMapper';
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor';
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper';
import vtkPolyData from '@kitware/vtk.js/Common/DataModel/PolyData';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import { tubeGeometry, type CoronaryTube } from './coronary';

// vtk.js keeps an expanded position, normal and colour record per triangle vertex on the GPU.
const GPU_BYTES_PER_TRIANGLE = 3 * (6 * Float32Array.BYTES_PER_ELEMENT + 4);

/** The research centreline tube: its own opaque actor, coloured per station, never pickable. */
export class VTKCoronaryTube {
  readonly actor = vtkActor.newInstance();
  private readonly mapper = vtkMapper.newInstance();
  private data = vtkPolyData.newInstance();
  private key = '';
  private shown = false;

  constructor(private reserveMemory?: (bytes: number) => void) {
    this.mapper.setInputData(this.data);
    this.mapper.setScalarVisibility(true);
    this.mapper.setColorModeToDirectScalars();
    this.mapper.setScalarModeToUsePointData();
    this.mapper.set({ populateSelectionSettings: false });
    this.actor.setMapper(this.mapper);
    this.actor.setPickable(false);
    this.actor.setVisibility(false);
    const property = this.actor.getProperty();
    property.setInterpolationToPhong();
    property.setAmbient(0.25);
    property.setDiffuse(0.8);
    property.setSpecular(0.2);
    property.setSpecularPower(30);
  }

  /** Rebuilds only when the analysis or the colouring changes. */
  setTube(tube: CoronaryTube | undefined) {
    const key = tube ? `${tube.result.analysis_id}:${tube.colouring}` : '';
    if (key !== this.key) {
      // A refused memory reservation throws here and leaves the previous tube as it was.
      this.replace(tube);
      this.key = key;
    }
    this.shown = !!tube;
    this.actor.setVisibility(this.shown);
  }
  /** Captures leave the tube out, then put it back as it was. */
  hideFor<T>(work: () => Promise<T>): Promise<T> {
    if (!this.shown) return work();
    this.actor.setVisibility(false);
    return work().finally(() => this.actor.setVisibility(this.shown));
  }
  get visible() {
    return this.shown;
  }
  private replace(tube: CoronaryTube | undefined) {
    const geometry = tube && tubeGeometry(tube);
    // Reserve first: a refusal throws before anything is replaced.
    this.reserveMemory?.(
      geometry
        ? geometry.positions.byteLength +
            geometry.normals.byteLength +
            geometry.colours.byteLength +
            geometry.polys.byteLength +
            geometry.triangles * GPU_BYTES_PER_TRIANGLE
        : 0,
    );
    const next = vtkPolyData.newInstance();
    if (geometry) {
      next.getPoints().setData(geometry.positions, 3);
      next.getPolys().setData(geometry.polys);
      next.getPointData().setNormals(vtkDataArray.newInstance({ name: 'Normals', values: geometry.normals, numberOfComponents: 3 }));
      next.getPointData().setScalars(vtkDataArray.newInstance({ name: 'Colours', values: geometry.colours, numberOfComponents: 3 }));
    }
    this.mapper.setInputData(next);
    this.data.delete();
    this.data = next;
  }
  dispose() {
    this.actor.delete();
    this.mapper.delete();
    this.data.delete();
    this.reserveMemory?.(0);
  }
}
