from __future__ import annotations

import numpy as np
import pytest

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



def test_shared_transform_engine_supports_zero_weight_exclusion_for_every_model():
    source=_grid(5)

    translation=np.repeat(np.array([[0.3],[-0.2]]),source.shape[1],axis=1)
    corrupted=np.array(translation,copy=True)
    corrupted[:,-1]=np.array([50.0,-40.0])
    weights=np.ones(source.shape[1]); weights[-1]=0.0
    fitted=fit_transform2d(source,corrupted,model="translation",weights=weights)
    np.testing.assert_allclose(
        fitted.displacement(source),translation,atol=1e-12,
    )

    angle=np.deg2rad(13.0)
    rotation=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    target=rotation@source+np.array([[0.12],[-0.07]])
    corrupted_target=np.array(target,copy=True); corrupted_target[:,-1]+=20.0
    fitted=fit_transform2d(
        source,corrupted_target-source,model="rigid",weights=weights,
    )
    np.testing.assert_allclose(fitted.apply(source),target,atol=1e-12)

    linear=np.array([[1.05,0.04],[-0.02,0.96]])
    target=linear@source+np.array([[0.08],[0.03]])
    corrupted_target=np.array(target,copy=True); corrupted_target[:,-1]-=30.0
    fitted=fit_transform2d(
        source,corrupted_target-source,model="affine",weights=weights,
    )
    np.testing.assert_allclose(fitted.apply(source),target,atol=1e-12)

    x,y=source
    displacement=np.vstack([
        0.1+0.03*x-0.02*y+0.04*x*x+0.01*x*y,
        -0.08+0.02*y+0.01*x+0.03*y*y-0.008*x*y,
    ])
    corrupted=np.array(displacement,copy=True); corrupted[:,-1]+=100.0
    fitted=fit_transform2d(
        source,corrupted,model="polynomial",polynomial_degree=2,weights=weights,
    )
    np.testing.assert_allclose(fitted.displacement(source),displacement,atol=1e-11)


def test_shared_transform_weights_are_validated_and_translation_is_weighted():
    source=np.array([[0.0,1.0,2.0],[0.0,0.0,0.0]])
    displacement=np.array([[0.0,2.0,10.0],[0.0,0.0,0.0]])
    fitted=fit_transform2d(
        source,displacement,model="translation",weights=np.array([1.0,3.0,0.0]),
    )
    np.testing.assert_allclose(fitted.matrix[:,2],np.array([1.5,0.0]))
    assert fitted.rms_residual==pytest.approx(np.sqrt((1.0*1.5**2+3.0*0.5**2)/4.0))

    with pytest.raises(ValueError,match="shape"):
        fit_transform2d(source,displacement,model="translation",weights=(1.0,2.0))
    with pytest.raises(ValueError,match=">= 0"):
        fit_transform2d(source,displacement,model="translation",weights=(1.0,-1.0,1.0))
    with pytest.raises(ValueError,match="positive-weight samples"):
        fit_transform2d(
            source,displacement,model="affine",weights=(1.0,1.0,0.0),
        )
