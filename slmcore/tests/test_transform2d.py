from __future__ import annotations

import numpy as np

from slmcore.core.transform2d import (
    FittedTransform2D,fit_transform2d,minimum_points_for_transform,
)


def _grid(n=5):
    values=np.linspace(-1.0,1.0,n)
    xx,yy=np.meshgrid(values,values,indexing="xy")
    return np.vstack([xx.ravel(),yy.ravel()])


def test_shared_transform_engine_translation_rigid_and_affine():
    source=_grid(3)

    translation=np.array([[0.3],[-0.2]])
    field=fit_transform2d(
        source,np.repeat(translation,source.shape[1],axis=1),model="translation",
    )
    np.testing.assert_allclose(
        field.displacement(source),
        np.repeat(translation,source.shape[1],axis=1),atol=1e-12,
    )

    angle=np.deg2rad(17.0)
    rotation=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    target=rotation@source+np.array([[0.12],[-0.08]])
    field=fit_transform2d(source,target-source,model="rigid")
    np.testing.assert_allclose(field.apply(source),target,atol=1e-12)
    np.testing.assert_allclose(field.matrix[:,:2].T@field.matrix[:,:2],np.eye(2),atol=1e-12)

    linear=np.array([[1.08,0.06],[-0.03,0.94]])
    target=linear@source+np.array([[0.05],[0.02]])
    field=fit_transform2d(source,target-source,model="affine")
    np.testing.assert_allclose(field.apply(source),target,atol=1e-12)


def test_shared_polynomial_engine_supports_orders_2_3_4_and_roundtrip():
    source=_grid(6)
    x,y=source
    displacement=np.vstack([
        0.1+0.03*x-0.02*y+0.04*x*x+0.01*x*y+0.006*x**3-0.002*x*y**2+0.001*x**4,
        -0.08+0.02*y+0.01*x+0.03*y*y-0.008*x*y+0.004*y**3+0.001*x**2*y**2,
    ])
    for degree in (2,3,4):
        field=fit_transform2d(
            source,displacement,model="polynomial",polynomial_degree=degree,
        )
        assert field.polynomial_degree==degree
        assert field.rms_residual>=0.0
        restored=FittedTransform2D.from_dict(field.to_dict())
        np.testing.assert_allclose(restored.displacement(source),field.displacement(source))
    degree4=fit_transform2d(
        source,displacement,model="polynomial",polynomial_degree=4,
    )
    np.testing.assert_allclose(degree4.displacement(source),displacement,atol=1e-11)


def test_shared_transform_minimum_point_contract():
    assert minimum_points_for_transform("translation")==1
    assert minimum_points_for_transform("rigid")==2
    assert minimum_points_for_transform("affine")==3
    assert minimum_points_for_transform("polynomial",2)==6
    assert minimum_points_for_transform("polynomial",3)==10
    assert minimum_points_for_transform("polynomial",4)==15
