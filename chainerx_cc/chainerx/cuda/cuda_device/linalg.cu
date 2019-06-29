#include "chainerx/cuda/cuda_device.h"

#include <cstdint>
#include <mutex>
#include <type_traits>

#include <cublas_v2.h>
#include <cuda_runtime.h>
#include <cusolverDn.h>
#include <cuda_fp16.hpp>

#include "chainerx/array.h"
#include "chainerx/axes.h"
#include "chainerx/backend.h"
#include "chainerx/backend_util.h"
#include "chainerx/cuda/cublas.h"
#include "chainerx/cuda/cuda_runtime.h"
#include "chainerx/cuda/cuda_set_device_scope.h"
#include "chainerx/cuda/cusolver.h"
#include "chainerx/cuda/data_type.cuh"
#include "chainerx/cuda/float16.cuh"
#include "chainerx/cuda/kernel_regist.h"
#include "chainerx/device.h"
#include "chainerx/dtype.h"
#include "chainerx/error.h"
#include "chainerx/float16.h"
#include "chainerx/kernels/creation.h"
#include "chainerx/kernels/linalg.h"
#include "chainerx/kernels/misc.h"
#include "chainerx/macro.h"
#include "chainerx/native/native_device.h"
#include "chainerx/routines/creation.h"
#include "chainerx/routines/linalg.h"

namespace chainerx {
namespace cuda {

class CudaSolveKernel : public SolveKernel {
public:
    void Call(const Array& a, const Array& b, const Array& out) override {
        Device& device = a.device();
        Dtype dtype = a.dtype();
        CudaSetDeviceScope scope{device.index()};

        CHAINERX_ASSERT(a.ndim() == 2);
        CHAINERX_ASSERT(a.shape()[0] == a.shape()[1]);

        auto solve_impl = [&](auto pt, auto getrf_bufferSize, auto getrf, auto getrs) {
            using T = typename decltype(pt)::type;
            cuda_internal::DeviceInternals& device_internals = cuda_internal::GetDeviceInternals(static_cast<CudaDevice&>(device));

            Array lu_matrix = Empty(a.shape(), dtype, device);
            device.backend().CallKernel<CopyKernel>(a.Transpose(), lu_matrix);
            T* lu_ptr = static_cast<T*>(internal::GetRawOffsetData(lu_matrix));

            int m = a.shape()[0];

            Array ipiv = Empty(Shape{m}, Dtype::kInt32, device);
            int* ipiv_ptr = static_cast<int*>(internal::GetRawOffsetData(ipiv));

            int buffersize = 0;
            device_internals.cusolverdn_handle().Call(getrf_bufferSize, m, m, lu_ptr, m, &buffersize);

            Array work = Empty(Shape{buffersize}, dtype, device);
            T* work_ptr = static_cast<T*>(internal::GetRawOffsetData(work));

            std::shared_ptr<void> devInfo = device.Allocate(sizeof(int));

            device_internals.cusolverdn_handle().Call(getrf, m, m, lu_ptr, m, work_ptr, ipiv_ptr, static_cast<int*>(devInfo.get()));

            int devInfo_h = 0;
            Device& native_device = dynamic_cast<native::NativeDevice&>(GetDefaultContext().GetDevice({"native", 0}));
            device.MemoryCopyTo(&devInfo_h, devInfo.get(), sizeof(int), native_device);
            if (devInfo_h != 0) {
                throw ChainerxError{"Unsuccessfull getrf (LU) execution. Info = ", devInfo_h};
            }

            device.backend().CallKernel<CopyKernel>(b, out);
            T* out_ptr = static_cast<T*>(internal::GetRawOffsetData(out));

            device_internals.cusolverdn_handle().Call(
                    getrs, CUBLAS_OP_N, m, m, lu_ptr, m, ipiv_ptr, out_ptr, m, static_cast<int*>(devInfo.get()));

            device.MemoryCopyTo(&devInfo_h, devInfo.get(), sizeof(int), native_device);
            if (devInfo_h != 0) {
                throw ChainerxError{"Unsuccessfull getrs (Solve) execution. Info = ", devInfo_h};
            }
        };

        switch (a.dtype()) {
            case Dtype::kFloat16:
                throw DtypeError{"Half-precision (float16) is not supported by solve"};
                break;
            case Dtype::kFloat32:
                solve_impl(PrimitiveType<float>{}, cusolverDnSgetrf_bufferSize, cusolverDnSgetrf, cusolverDnSgetrs);
                break;
            case Dtype::kFloat64:
                solve_impl(PrimitiveType<double>{}, cusolverDnDgetrf_bufferSize, cusolverDnDgetrf, cusolverDnDgetrs);
                break;
            default:
                CHAINERX_NEVER_REACH();
        }
    }
};

CHAINERX_CUDA_REGISTER_KERNEL(SolveKernel, CudaSolveKernel);

class CudaInverseKernel : public InverseKernel {
public:
    void Call(const Array& a, const Array& out) override {
        Device& device = a.device();
        Dtype dtype = a.dtype();
        CudaSetDeviceScope scope{device.index()};

        CHAINERX_ASSERT(a.ndim() == 2);
        CHAINERX_ASSERT(a.shape()[0] == a.shape()[1]);

        // There is LAPACK routine ``getri`` for computing the inverse of an LU-factored matrix,
        // but cuSOLVER does not have it implemented, therefore inverse is obtained with ``getrs``
        // inv(A) == solve(A, Identity)
        Array b = Identity(a.shape()[0], dtype, device);
        device.backend().CallKernel<SolveKernel>(a.Transpose(), b, out);
    }
};

CHAINERX_CUDA_REGISTER_KERNEL(InverseKernel, CudaInverseKernel);

}  // namespace cuda
}  // namespace chainerx
